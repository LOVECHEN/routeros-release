#!/usr/bin/env python3
"""从 MikroTik 官方下载页发现并校验文件，按版本镜像到 GitHub Releases。"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

SITE = ""
CDN = ""
CHANNELS = ("longTerm", "stable", "testing", "development")
VERSION_RE = re.compile(r"[67]\.\d+(?:\.\d+)?(?:(?:beta|rc)\d+)?")
HASH_RE = re.compile(r"[0-9a-f]{64}")
METADATA = {"checksums.sha256", "sources.json"}


def configure_sources():
    global SITE, CDN
    SITE = os.environ.get("ROUTEROS_SITE", "").strip().rstrip("/")
    CDN = os.environ.get("ROUTEROS_CDN", "").strip().rstrip("/")
    for name, value in (("ROUTEROS_SITE", SITE), ("ROUTEROS_CDN", CDN)):
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"缺少有效的 {name}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::add-mask::{value}", flush=True)
            print(f"::add-mask::{parsed.hostname}", flush=True)


def validate_version(value: str) -> str:
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f"无效的 RouterOS 版本: {value}")
    return value


def unpack(value):
    """去掉 Livewire 的 array/wireable 类型元数据，保留业务数据。"""
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[1], dict) and "s" in value[1]:
            return unpack(value[0])
        return [unpack(item) for item in value]
    if isinstance(value, dict):
        return {key: unpack(item) for key, item in value.items()}
    return value


class Page(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.snapshots = {}
        self.files = []
        self.architectures = set()
        self.csrf = ""
        self.update_url = ""
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "wire:snapshot" in attrs:
            raw = attrs["wire:snapshot"]
            snapshot = json.loads(raw)
            name = snapshot["memo"]["name"]
            self.snapshots[name] = raw
            if name == "components.software.release-file-download":
                data = unpack(snapshot["data"])
                self.files.append({**data["releaseFile"], "version": data["version"]})
        if "data-csrf" in attrs:
            self.csrf = attrs["data-csrf"]
        if "data-update-uri" in attrs:
            self.update_url = urllib.parse.urljoin(SITE, attrs["data-update-uri"])
        match = re.fullmatch(r"selectArchitecture\('([a-z0-9]+)'\)", attrs.get("wire:click", ""))
        if match:
            self.architectures.add(match[1])

    def component(self, name):
        return unpack(json.loads(self.snapshots[name])["data"])


class HTTP:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def open(self, url, *, data=None, headers=None, method=None):
        for attempt in range(4):
            request = urllib.request.Request(
                url, data=data, method=method,
                headers={"User-Agent": "routeros-release/1.0", **(headers or {})},
            )
            try:
                return self.opener.open(request, timeout=120)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                    delay = max(5, int(exc.headers.get("X-RateLimit-Reset", 0)) - time.time() + 5)
                    print(f"API rate limit: waiting {int(delay)} seconds", flush=True)
                    time.sleep(delay)
                    continue
                if exc.code in (403, 429):
                    detail = exc.read().decode("utf-8", "replace").lower()
                    if exc.headers.get("Retry-After") or "secondary rate limit" in detail:
                        delay = max(90, int(exc.headers.get("Retry-After", 0))) + random.randint(0, 15)
                        print(f"API throttled: waiting {delay} seconds", flush=True)
                        time.sleep(delay)
                        continue
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except (OSError, urllib.error.URLError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError("HTTP retry limit exceeded")

    def text(self, url):
        with self.open(url) as response:
            return response.read().decode("utf-8")


COMPONENTS = {
    "routeros": "components.software.router-OS",
    "chr": "components.software.cloud-hosted-router",
}


def load_page(product="routeros", architecture="x86", version=None):
    http = HTTP()
    query = urllib.parse.urlencode({"architecture": architecture}) if product == "routeros" else ""
    page = Page(http.text(f"{SITE}/download/{product}?{query}"))
    component = COMPONENTS[product]
    current = page.component(component)
    if version and current["version"] != version:
        if urllib.parse.urlsplit(page.update_url).netloc != urllib.parse.urlsplit(SITE).netloc or not page.csrf:
            raise ValueError("官方下载页缺少有效的 Livewire 更新入口")
        payload = {
            "_token": page.csrf,
            "components": [{"snapshot": page.snapshots[component],
                            "updates": {"version": version}, "calls": []}],
        }
        with http.open(page.update_url, data=json.dumps(payload).encode(),
                       headers={"Content-Type": "application/json", "X-Livewire": ""}) as response:
            result = json.load(response)["components"][0]
        updated = Page(result["effects"]["html"])
        updated.snapshots[component] = result["snapshot"]
        page = updated
    if version and page.component(component)["version"] != version:
        raise ValueError(f"官方页面未切换到请求的版本 {version}")
    return page


def historical_catalog(source):
    matches = list(re.finditer(r'data-changelog-version="([^"]+)"', source))
    result = {}
    labels = {"longTerm": "Long-term", "stable": "Stable", "testing": "Testing", "development": "Development"}
    for index, match in enumerate(matches):
        version = match[1]
        if not VERSION_RE.fullmatch(version):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        section = source[match.end():end]
        date = re.search(r"\b\d{4}-\d{2}-\d{2}\b", section)
        if not date:
            raise ValueError(f"历史版本缺少发布日期: {version}")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", section))
        result[version] = {"version": version, "publishedAt": date[0] + "T00:00:00Z", "archived": True,
                           "channels": {key: bool(re.search(r"\b" + label + r"\b", text)) for key, label in labels.items()}}
    if not result:
        raise ValueError("官方历史版本清单为空")
    return result


def catalog(page, include_history=False):
    releases = page.component(COMPONENTS["routeros"])["releases"]
    if not releases:
        raise ValueError("官方版本清单为空")
    result = {}
    for release in releases:
        version = validate_version(release["version"])
        datetime.fromisoformat(release["publishedAt"].replace("Z", "+00:00"))
        result[version] = release
    if include_history:
        history = historical_catalog(HTTP().text(f"{SITE}/download/changelogs?versionFilter=&channelFilter="))
        for version, release in history.items():
            result.setdefault(version, release)
    return result


def select_releases(releases, versions="", channels=CHANNELS, all_versions=False):
    if all_versions:
        selected = list(releases.values())
    elif versions:
        wanted = list(dict.fromkeys(validate_version(v.strip()) for v in versions.split(",")))
        unknown = set(wanted) - releases.keys()
        if unknown:
            raise ValueError(f"官方版本清单中不存在: {', '.join(sorted(unknown))}")
        selected = [releases[v] for v in wanted]
    else:
        selected = {}
        for major in ("6", "7"):
            for channel in channels:
                candidates = [r for v, r in releases.items()
                              if v.split(".")[0] == major and not r["archived"]
                              and r["channels"].get(channel)]
                if candidates:
                    release = max(candidates, key=lambda r: r["publishedAt"])
                    selected[release["version"]] = release
        selected = list(selected.values())
    return sorted(selected, key=lambda r: r["publishedAt"])


def normalize_files(page, version):
    result = {}
    for source in page.files:
        if source["version"] != version:
            continue
        name = source["name"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name in METADATA:
            raise ValueError(f"无效的官方文件名: {name}")
        digest = source["checksumSHA256"].lower()
        if not HASH_RE.fullmatch(digest) or int(source["size"]) <= 0:
            raise ValueError(f"官方文件信息不完整: {name}")
        item = {"name": name, "size": int(source["size"]), "sha256": digest,
                "url": f"{CDN}/{version}/{name}", "key": source["key"],
                "architecture": source["architecture"]}
        if name in result and result[name] != item:
            raise ValueError(f"官方清单中文件信息冲突: {name}")
        result[name] = item
    if not result:
        raise ValueError(f"官方文件清单为空: {version}")
    return result


def discover_files(version, workers=4):
    base = load_page(version=version)
    architectures = base.architectures
    if not architectures or "x86" not in architectures:
        raise ValueError("无法发现 RouterOS 架构清单")
    pages = [base]
    tasks = [("routeros", arch) for arch in sorted(architectures - {"x86"})]
    tasks.append(("chr", "x86"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pages.extend(pool.map(lambda task: load_page(*task, version=version), tasks))
    files = {}
    for page in pages:
        for name, item in normalize_files(page, version).items():
            if name in files and (files[name]["sha256"], files[name]["size"]) != (item["sha256"], item["size"]):
                raise ValueError(f"不同架构页面的文件校验冲突: {name}")
            files.setdefault(name, item)
    return [files[name] for name in sorted(files)]


def download_verified(item, directory):
    path = directory / item["name"]
    for attempt in range(3):
        digest = hashlib.sha256()
        try:
            with HTTP().open(item["url"]) as response, path.open("wb") as target:
                if urllib.parse.urlsplit(response.geturl()).hostname != urllib.parse.urlsplit(CDN).hostname:
                    raise ValueError("下载跳转到非官方来源")
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(block)
                    target.write(block)
            if path.stat().st_size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"官方 SHA-256 或文件大小不匹配: {item['name']}")
            return path
        except (OSError, ValueError, urllib.error.URLError):
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", self.repo):
            raise ValueError("无效的 GITHUB_REPOSITORY")
        self.token = os.environ["GH_TOKEN"]
        self.http = HTTP()

    def api(self, path, data=None, method=None):
        url = f"https://api.github.com/repos/{self.repo}/{path}"
        payload = json.dumps(data).encode() if data is not None else None
        with self.http.open(url, data=payload, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
        }) as response:
            body = response.read()
            return json.loads(body) if body else None

    def release(self, tag):
        # 分批回填时只加载一次索引，索引同时包含已发布 Release 和 draft。
        if getattr(self, "_release_cache", None) is None:
            self._release_cache = {}
            for release in self.releases():
                if release["tag_name"] in self._release_cache:
                    raise ValueError(f"同一 tag 存在多个 Release: {release['tag_name']}")
                self._release_cache[release["tag_name"]] = release
        return self._release_cache.get(tag)

    def assets(self, release_id):
        assets = {}
        page = 1
        while True:
            batch = self.api(f"releases/{release_id}/assets?per_page=100&page={page}")
            assets.update({item["name"]: item for item in batch})
            if len(batch) < 100:
                return assets
            page += 1

    def dated_tag(self, tag, published):
        try:
            self.api(f"git/ref/tags/{tag}")
            return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        head = os.environ.get("GITHUB_SHA") or self.api("commits/main")["sha"]
        if getattr(self, "_tree", None) is None:
            self._tree = self.api(f"git/commits/{head}")["tree"]["sha"]
        tree = self._tree
        identity = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": published}
        commit = self.api("git/commits", {"message": f"archive: {tag}", "tree": tree,
                                         "parents": [head], "author": identity, "committer": identity})
        self.api("git/refs", {"ref": f"refs/tags/{tag}", "sha": commit["sha"]})

    def releases(self):
        result = []
        page = 1
        while True:
            batch = self.api(f"releases?per_page=100&page={page}")
            result.extend(batch)
            if len(batch) < 100:
                return result
            page += 1

    def upload(self, release_id, path, existing=None):
        # gh 使用流式上传，避免把大型镜像读入 Python 内存。
        url = (f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets?"
               + urllib.parse.urlencode({"name": path.name}))
        for attempt in range(3):
            if existing:
                self.api(f"releases/assets/{existing['id']}", method="DELETE")
            try:
                subprocess.run(["gh", "api", "--method", "POST", url, "--input", str(path),
                                "--header", "Content-Type: application/octet-stream"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                return
            except subprocess.CalledProcessError as exc:
                if attempt == 2:
                    raise
                existing = self.assets(release_id).get(path.name)
                time.sleep(90 if "rate limit" in (exc.stderr or "").lower() else 2 ** attempt)


def asset_matches(asset, item):
    return (asset.get("state") == "uploaded" and asset.get("size") == item["size"]
            and asset.get("digest") == f"sha256:{item['sha256']}")


def metadata_bytes(release, files):
    checksums = "".join(f"{f['sha256']}  {f['name']}\n" for f in files).encode()
    return {"checksums.sha256": checksums}


def release_notes(release, files):
    lines = ["| SHA-256 | 文件 / File |", "|---|---|"]
    lines.extend(f"| `{f['sha256']}` | `{f['name']}` |" for f in files)
    return "\n".join(lines) + "\n"


def refresh_releases(github):
    for release in github.releases():
        if not release["tag_name"].startswith("routeros-"):
            continue
        assets = github.assets(release["id"])
        if "sources.json" in assets:
            github.api(f"releases/assets/{assets['sources.json']['id']}", method="DELETE")
        files = []
        for name, asset in sorted(assets.items()):
            if name in METADATA:
                continue
            digest = asset.get("digest", "").removeprefix("sha256:")
            if not HASH_RE.fullmatch(digest):
                raise ValueError(f"附件缺少 SHA-256: {name}")
            files.append({"name": name, "sha256": digest})
        github.api(f"releases/{release['id']}", {"body": release_notes(release, files)}, method="PATCH")
        print(f"Refreshed {release['tag_name']}", flush=True)


def mirror(github, release, files, latest, workers=4):
    version = release["version"]
    tag = f"routeros-{version}"
    existing = github.release(tag)
    prerelease = bool(re.search(r"beta|rc", version))
    if not existing:
        github.dated_tag(tag, release["publishedAt"])
        existing = github.api("releases", {"tag_name": tag, "name": f"RouterOS {version}",
                                          "draft": True, "prerelease": prerelease})
    assets = github.assets(existing["id"])
    if "sources.json" in assets:
        github.api(f"releases/assets/{assets.pop('sources.json')['id']}", method="DELETE")
    missing = [item for item in files if not asset_matches(assets.get(item["name"], {}), item)]
    print(f"{tag}: {len(files)} files, {len(missing)} missing or unverified", flush=True)
    metadata = metadata_bytes(release, files)
    expected = list(files)
    with tempfile.TemporaryDirectory(prefix="routeros-release-") as directory:
        directory = Path(directory)
        # 并发数限制磁盘占用；每个文件上传后立即释放，中断后的 draft 可继续补齐。
        def transfer(item):
            path = download_verified(item, directory)
            try:
                github.upload(existing["id"], path, assets.get(item["name"]))
                print(f"Uploaded {tag}: {item['name']}", flush=True)
            finally:
                path.unlink(missing_ok=True)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(transfer, missing))
        for name, content in metadata.items():
            item = {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
            expected.append(item)
            if not asset_matches(assets.get(name, {}), item):
                path = directory / name
                path.write_bytes(content)
                github.upload(existing["id"], path, assets.get(name))
    verified = github.assets(existing["id"])
    for item in expected:
        if not asset_matches(verified.get(item["name"], {}), item):
            raise ValueError(f"GitHub 附件核验失败: {item['name']}")
    github.api(f"releases/{existing['id']}", {
        "name": f"RouterOS {version}", "body": release_notes(release, files),
        "draft": False, "prerelease": prerelease, "make_latest": "true" if latest else "false",
    }, method="PATCH")
    print(f"Published: https://github.com/{github.repo}/releases/tag/{tag}", flush=True)


def keepalive():
    timestamp = subprocess.check_output(["git", "log", "-1", "--format=%ct"], text=True).strip()
    if time.time() - int(timestamp) < 50 * 86400:
        print("Keepalive: repository active")
        return
    subprocess.run(["git", "-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit", "--allow-empty", "-m", "chore: keep scheduled mirror active"], check=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "matrix", "sync", "refresh", "keepalive", "jitter"))
    parser.add_argument("--versions", default="", help="逗号分隔的官方历史版本；留空同步当前渠道")
    parser.add_argument("--channels", default=",".join(CHANNELS))
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    parser.add_argument("--files", action="store_true", help="plan 时同时检查完整文件清单")
    parser.add_argument("--all", action="store_true", help="处理官方清单中的全部历史版本")
    args = parser.parse_args()
    if args.command == "keepalive":
        keepalive()
        return
    if args.command == "jitter":
        time.sleep(random.SystemRandom().randint(0, 1800))
        return
    if args.command == "refresh":
        refresh_releases(GitHub())
        return
    configure_sources()
    channels = args.channels.split(",")
    if not set(channels) <= set(CHANNELS):
        parser.error("无效的渠道名称")
    releases = catalog(load_page(), include_history=args.all or bool(args.versions))
    selected = select_releases(releases, args.versions, channels, args.all)
    if args.command == "matrix":
        selected.reverse()
        batches = [selected[i:i + 16] for i in range(0, len(selected), 16)]
        matrix = {"include": [{"batch": i + 1, "versions": ",".join(r["version"] for r in batch)}
                               for i, batch in enumerate(batches)]}
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write("matrix=" + json.dumps(matrix) + "\n")
        print(f"Planned {len(selected)} versions in {len(batches)} batches", flush=True)
        return
    stable = [r for v, r in releases.items() if v.startswith("7.") and r["channels"].get("stable")
              and not r["archived"] and not re.search(r"beta|rc", v)]
    latest = max(stable, key=lambda r: r["publishedAt"])["version"] if stable else None
    github = GitHub() if args.command == "sync" else None
    failures = []
    for release in selected:
        version = release["version"]
        try:
            print(json.dumps(release, ensure_ascii=False), flush=True)
            if github or args.files:
                files = discover_files(version, args.workers)
                print(f"{version}: {len(files)} files, {sum(f['size'] for f in files) / 1048576:.1f} MiB", flush=True)
                if github:
                    mirror(github, release, files, version == latest, args.workers)
        except Exception as exc:
            failures.append(version)
            print(f"ERROR {version}: {exc}", flush=True)
    if failures:
        raise SystemExit(f"同步失败的版本: {', '.join(failures)}")


if __name__ == "__main__":
    main()
