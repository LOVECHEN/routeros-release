#!/usr/bin/env python3
"""从 MikroTik 官方下载页发现并校验文件，按版本镜像到 GitHub Releases。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.cookiejar
import json
import os
import random
import re
import subprocess
import tempfile
import threading
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
# 用户确认跳过的单个上游异常文件；其他文件仍须严格核验。
EXCLUDED_FILES = {"6.46.4": frozenset({"chr-6.46.4.img.zip"})}


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
        attempt = throttles = 0
        while attempt < 4:
            request = urllib.request.Request(
                url, data=data, method=method,
                headers={"User-Agent": "routeros-release/1.0", **(headers or {})},
            )
            try:
                return self.opener.open(request, timeout=120)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    detail = exc.read().decode("utf-8", "replace")
                    delay = throttle_delay(exc.headers, detail, throttles)
                    if delay is not None and throttles < 12:
                        throttles += 1
                        print(f"API throttled: waiting {delay} seconds", flush=True)
                        time.sleep(delay)
                        continue
                    try:
                        exc.reason = json.loads(detail).get("message", exc.reason)
                    except (ValueError, AttributeError):
                        pass
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except (OSError, urllib.error.URLError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)
            attempt += 1
        raise RuntimeError("HTTP retry limit exceeded")

    def text(self, url):
        with self.open(url, headers={"Accept-Encoding": "gzip"}) as response:
            body = response.read()
            if getattr(response, "headers", {}).get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return body.decode("utf-8")


def throttle_delay(headers, detail="", attempt=0):
    headers = {key.lower(): str(value) for key, value in headers.items()}
    if headers.get("x-ratelimit-remaining") == "0":
        return max(5, int(headers.get("x-ratelimit-reset", time.time() + 60)) - int(time.time()) + 5)
    if headers.get("retry-after"):
        return max(1, int(headers["retry-after"])) + 5
    if "rate limit" in detail.lower() or "throttl" in detail.lower():
        return min(900, 90 * 2 ** attempt)
    return None


class UpstreamUnavailable(ValueError):
    pass


class IntegrityError(ValueError):
    pass


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


def current_releases(page):
    releases = page.component(COMPONENTS["routeros"])["releases"]
    if not releases:
        raise ValueError("官方版本清单为空")
    result = {}
    for release in releases:
        version = validate_version(release["version"])
        datetime.fromisoformat(release["publishedAt"].replace("Z", "+00:00"))
        result[version] = release
    return result


def select_releases(releases):
    selected = {}
    for major in ("6", "7"):
        for channel in CHANNELS:
            candidates = [r for v, r in releases.items()
                          if v.split(".")[0] == major and not r["archived"]
                          and r["channels"].get(channel)]
            if candidates:
                release = max(candidates, key=lambda r: r["publishedAt"])
                selected[release["version"]] = release
    retained = retained_previews(releases)
    return sorted((r for r in selected.values() if not re.search(r"beta|rc", r["version"])
                   or r["version"] in retained), key=lambda r: r["publishedAt"])


def preview_heads(releases):
    heads = set()
    for major in ("6", "7"):
        for channel in ("testing", "development"):
            candidates = [r for v, r in releases.items() if v.startswith(major + ".")
                          and not r.get("archived", False) and r["channels"].get(channel)]
            if candidates:
                heads.add(max(candidates, key=lambda r: r["publishedAt"])["version"])
    return heads


def version_number(version):
    parts = tuple(map(int, re.split(r"beta|rc", version)[0].split(".")))
    return parts + (0,) * (3 - len(parts))


def preview_superseded(version, releases):
    if not re.search(r"beta|rc", version):
        return False
    target = version_number(version)
    return any(not re.search(r"beta|rc", final) and version_number(final)[0] == target[0]
               and version_number(final) >= target
               and any(info["channels"].get(channel) for channel in ("stable", "longTerm"))
               for final, info in releases.items())


def retained_previews(releases):
    return {version for version in preview_heads(releases) if re.search(r"beta|rc", version)
            and not preview_superseded(version, releases)}


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
        if not architectures and not base.files and not base.component(COMPONENTS["routeros"]).get("architecture"):
            raise UpstreamUnavailable(f"官方未提供 {version} 的文件和架构清单")
        raise ValueError(f"无法发现 {version} 的 RouterOS 架构清单")
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


def response_size(response):
    if urllib.parse.urlsplit(response.geturl()).hostname != urllib.parse.urlsplit(CDN).hostname:
        raise ValueError("下载跳转到非官方来源")
    headers = {key.lower(): value for key, value in response.headers.items()}
    if headers.get("content-encoding", "identity").lower() != "identity":
        return None
    value = headers.get("content-length", "")
    return int(value) if re.fullmatch(r"[0-9]+", value) and int(value) > 0 else None


def correct_listed_size(item, size):
    old_size = item["size"]
    item["size"] = size
    print(f"Corrected stale listed size: {item['name']}; {old_size} -> {size} bytes "
          "(official response size and SHA-256 verified)", flush=True)


def download_verified(item, directory):
    path = directory / item["name"]
    for attempt in range(3):
        digest = hashlib.sha256()
        try:
            with HTTP().open(item["url"], headers={"Accept-Encoding": "identity"}) as response, path.open("wb") as target:
                official_size = response_size(response)
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(block)
                    target.write(block)
            actual_size = path.stat().st_size
            if (digest.hexdigest() != item["sha256"] or
                    official_size is not None and actual_size != official_size or
                    actual_size != item["size"] and actual_size != official_size):
                raise IntegrityError(f"官方文件校验不一致: {item['name']}; "
                                     f"expected={item['size']}/{item['sha256']}; "
                                     f"actual={actual_size}/{digest.hexdigest()}")
            # 网页可能保留旧大小；只在官方完整 SHA-256 和下载响应大小都匹配时更正。
            if actual_size != item["size"]:
                correct_listed_size(item, actual_size)
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
        self.token = os.environ.get("GH_TOKEN", "")
        self.http = HTTP()
        self._write_lock = threading.Lock()
        self._last_write = float("-inf")

    def wait_for_write(self):
        if not hasattr(self, "_write_lock"):
            self._write_lock = threading.Lock()
            self._last_write = float("-inf")
        with self._write_lock:
            delay = getattr(self, "_write_interval", 8) - (time.monotonic() - self._last_write)
            if delay > 0:
                time.sleep(delay)
            self._last_write = time.monotonic()

    def api(self, path, data=None, method=None):
        url = f"https://api.github.com/repos/{self.repo}/{path}"
        payload = json.dumps(data).encode() if data is not None else None
        if data is not None or method in ("POST", "PATCH", "PUT", "DELETE"):
            self.wait_for_write()
        with self.http.open(url, data=payload, method=method, headers={
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "gzip",
            "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
        }) as response:
            body = response.read()
            if getattr(response, "headers", {}).get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body) if body else None

    def release(self, tag):
        # 一次运行只加载一次索引，同时覆盖已发布 Release 和 draft。
        if getattr(self, "_release_cache", None) is None:
            grouped = {}
            for release in self.releases():
                version = release_version(release)
                release_tag = f"routeros-{version}" if version else release["tag_name"]
                grouped.setdefault(release_tag, {})[release["id"]] = release
            cache, conflicts = {}, set()
            for release_tag, records in grouped.items():
                published = [r for r in records.values() if not r.get("draft", False)]
                if len(published) > 1:
                    conflicts.add(release_tag)
                candidates = published or list(records.values())
                cache[release_tag] = max(candidates, key=lambda r: (
                    r["tag_name"] == release_tag, len(r.get("assets", [])), -r["id"]))
            self._release_cache = cache
            self._release_conflicts = conflicts
        if tag in getattr(self, "_release_conflicts", set()):
            raise ValueError(f"同一版本存在多个已发布 Release，需人工核对: {tag}")
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
        # main 可能已更新，始终使用当前树创建归档 Tag。
        head = self.api("commits/main")["sha"]
        if getattr(self, "_tree_head", None) != head:
            self._tree = self.api(f"git/commits/{head}")["tree"]["sha"]
            self._tree_head = head
        tree = self._tree
        identity = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": published}
        commit = self.api("git/commits", {"message": f"archive: {tag}", "tree": tree,
                                         "parents": [head], "author": identity, "committer": identity})
        self.api("git/refs", {"ref": f"refs/tags/{tag}", "sha": commit["sha"]})

    def releases(self):
        result = {}
        page = 1
        while True:
            batch = self.api(f"releases?per_page=100&page={page}")
            result.update({item["id"]: item for item in batch})
            if len(batch) < 100:
                return list(result.values())
            page += 1

    def upload(self, release_id, path, existing=None):
        # gh 使用流式上传，避免把大型镜像读入 Python 内存。
        url = (f"https://uploads.github.com/repos/{self.repo}/releases/{release_id}/assets?"
               + urllib.parse.urlencode({"name": path.name}))
        attempt = throttles = 0
        while attempt < 6:
            if existing:
                self.api(f"releases/assets/{existing['id']}", method="DELETE")
            try:
                self.wait_for_write()
                subprocess.run(["gh", "api", "--method", "POST", url, "--input", str(path),
                                "--header", "Content-Type: application/octet-stream", "--include"],
                               check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                return
            except subprocess.CalledProcessError as exc:
                output = exc.stdout or ""
                headers = dict(re.findall(r"^([\w-]+):\s*(.*?)\r?$", output, re.MULTILINE))
                delay = throttle_delay(headers, (exc.stderr or "") + output, throttles)
                if delay is not None and throttles < 12:
                    throttles += 1
                    print(f"Upload throttled: waiting {delay} seconds", flush=True)
                    time.sleep(delay)
                elif attempt == 5:
                    raise RuntimeError(f"GitHub 上传失败: {path.name}; {(exc.stderr or 'request failed').strip()[:500]}") from exc
                else:
                    time.sleep(2 ** attempt)
                    attempt += 1
                existing = self.assets(release_id).get(path.name)
                if existing and existing.get("state") == "uploaded" and existing.get("size") == path.stat().st_size:
                    with path.open("rb") as source:
                        digest = hashlib.file_digest(source, "sha256").hexdigest()
                    if existing.get("digest") == "sha256:" + digest:
                        return


def asset_matches(asset, item):
    return (asset.get("state") == "uploaded" and asset.get("size") == item["size"]
            and asset.get("digest") == f"sha256:{item['sha256']}")


def metadata_bytes(release, files):
    checksums = "".join(f"{f['sha256']}  {f['name']}\n" for f in files).encode()
    return {"checksums.sha256": checksums}


def release_notes(release, files):
    version = release.get("version") or release_version(release)
    excluded = EXCLUDED_FILES.get(version, frozenset())
    files = [item for item in files if item["name"] not in excluded]
    groups = group_files(files)
    lines = []
    if excluded:
        lines.extend(["已跳过 / Excluded: " + ", ".join(f"`{name}`" for name in sorted(excluded))
                      + "（官方 SHA-256 不一致 / upstream SHA-256 mismatch）。", ""])
    lines.extend(["Architecture: " + " · ".join(f"[{arch}](#{arch})" for arch in groups), ""])
    for arch, items in groups.items():
        lines.extend([f"### {arch}", "", "| SHA-256 | 文件 / File |", "|---|---|"])
        lines.extend(f"| `{f['sha256']}` | [{f['name']}]({download_url(release, f['name'])}) |" for f in items)
        lines.append("")
    return "\n".join(lines) + "\n"


ARCHITECTURES = ("arm64", "arm", "mipsbe", "mmips", "ppc", "smips", "tile", "mipsle", "x86", "chr-arm64", "chr-x86_64", "tools")


def architecture(item):
    name = item["name"].lower()
    if name.startswith("chr-"):
        return "chr-arm64" if "arm64" in name else "chr-x86_64"
    if name.startswith(("netinstall", "dude-install", "btest", "flashfig")) or name.endswith(".mib"):
        return "tools"
    match = re.search(r"(?:^|[-_])(arm64|arm|mipsbe|mmips|powerpc|ppc|smips|tile|mipsle|x86)(?:[-_.]|$)", name)
    if match:
        return "ppc" if match[1] == "powerpc" else match[1]
    if name.startswith(("routeros-", "mikrotik-", "install-image-")) or name.endswith(".npk"):
        return "x86"
    return item.get("architecture") or "tools"


def group_files(files):
    groups = {}
    for item in sorted(files, key=lambda f: f["name"]):
        groups.setdefault(architecture(item), []).append(item)
    return {arch: groups[arch] for arch in ARCHITECTURES if arch in groups} | {
        arch: items for arch, items in groups.items() if arch not in ARCHITECTURES}


def download_url(release, name):
    tag = release.get("tag_name") or "routeros-" + release["version"]
    repo = os.environ.get("GITHUB_REPOSITORY", "LOVECHEN/routeros-release")
    return f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(name, safe='')}"


def release_title(release):
    channels = [c for c in CHANNELS if release["channels"].get(c)]
    return f"RouterOS {release['version']}" + (f" [{' '.join(channels)}]" if channels else "")


def draft_notes():
    repo = os.environ.get("GITHUB_REPOSITORY", "LOVECHEN/routeros-release")
    return ("未完成：附件尚未全部上传并通过校验，暂不发布。\n\n"
            f"此标记不代表任务正在运行，执行状态请查看 [Actions](https://github.com/{repo}/actions/workflows/sync.yml)。\n\n"
            "Incomplete mirror; this draft is not a published release or an indication of an active workflow.\n")


def draft_title(release):
    return release_title(release) + " [未完成]"


def release_version(release):
    tag = release["tag_name"]
    if tag.startswith("routeros-"):
        return validate_version(tag.removeprefix("routeros-"))
    if tag.startswith("untagged-"):
        match = re.fullmatch(r"RouterOS (" + VERSION_RE.pattern + r")(?: \[[^\]]+\])*", release.get("name", ""))
        if match:
            return match[1]
    return None


def latest_stable(history):
    stable = [r for v, r in history.items() if v.startswith("7.") and r["channels"].get("stable")
              and not r.get("archived", False) and not re.search(r"beta|rc", v)]
    return max(stable, key=lambda r: r["publishedAt"])["version"] if stable else None


def mirror(github, release, files, latest, workers=4):
    version = release["version"]
    excluded = EXCLUDED_FILES.get(version, frozenset())
    files = [item for item in files if item["name"] not in excluded]
    if not files:
        raise ValueError(f"没有可发布的文件: {version}")
    if excluded:
        print(f"Excluded by explicit exception: {', '.join(sorted(excluded))}", flush=True)
    tag = f"routeros-{version}"
    existing = github.release(tag)
    prerelease = bool(re.search(r"beta|rc", version))
    if not existing:
        github.dated_tag(tag, release["publishedAt"])
        existing = github.api("releases", {"tag_name": tag, "name": draft_title(release),
                                          "body": draft_notes(),
                                          "draft": True, "prerelease": prerelease})
    elif existing.get("tag_name") != tag:
        github.dated_tag(tag, release["publishedAt"])
    assets = github.assets(existing["id"])
    for name in excluded:
        if name in assets:
            github.api(f"releases/assets/{assets.pop(name)['id']}", method="DELETE")
    if "sources.json" in assets:
        github.api(f"releases/assets/{assets.pop('sources.json')['id']}", method="DELETE")
    for item in files:
        asset = assets.get(item["name"], {})
        if (asset.get("state") == "uploaded" and asset.get("digest") == "sha256:" + item["sha256"]
                and asset.get("size", 0) > 0 and asset["size"] != item["size"]):
            with HTTP().open(item["url"], method="HEAD", headers={"Accept-Encoding": "identity"}) as response:
                if response_size(response) == asset["size"]:
                    correct_listed_size(item, asset["size"])
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
        "tag_name": tag, "name": release_title(release), "body": release_notes(release, files),
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


def write_report(records):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write("## RouterOS sync\n\n| Version | Result |\n|---|---|\n")
            for record in records:
                output.write(f"| {record['version']} | {record['status']} |\n")
            output.write("\nUnverified versions remain drafts.\n")


def cleanup_previews(github, history):
    retained = retained_previews(history)
    heads = preview_heads(history)
    releases = github.releases()
    removed = 0
    for record in releases:
        version = release_version(record)
        if not version or not re.search(r"beta|rc", version):
            continue
        # 即使官方移除了旧测试版记录，明确的正式版版本号仍可证明其已被取代。
        if preview_superseded(version, history) or (heads and version in history and version not in retained):
            github.api(f"releases/{record['id']}", method="DELETE")
            removed += 1
            print(f"Removed obsolete preview: {version}", flush=True)
    print(f"Retained previews: {', '.join(sorted(retained))}; removed: {removed}", flush=True)


def prune_runs(github):
    current = os.environ.get("GITHUB_RUN_ID", "")
    candidates = []
    page = 1
    while True:
        batch = github.api(f"actions/runs?status=completed&per_page=100&page={page}")["workflow_runs"]
        candidates.extend(r["id"] for r in batch if r["status"] == "completed" and str(r["id"]) != current)
        if len(batch) < 100:
            break
        page += 1
    removed = 0
    for run_id in dict.fromkeys(candidates):
        try:
            # 重跑可能使已完成记录重新变成排队/运行中，删除前再次确认。
            if github.api(f"actions/runs/{run_id}")["status"] != "completed":
                continue
            github.api(f"actions/runs/{run_id}", method="DELETE")
            removed += 1
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 409):
                raise
    print(f"Removed {removed} completed workflow runs; active and queued runs retained", flush=True)
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sync", "prune-runs", "keepalive", "jitter"))
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args()
    if args.command == "keepalive":
        keepalive()
        return
    if args.command == "jitter":
        time.sleep(random.SystemRandom().randint(0, 1800))
        return
    if args.command == "prune-runs":
        github = GitHub()
        github._write_interval = 1
        prune_runs(github)
        return
    configure_sources()
    releases = current_releases(load_page())
    selected = select_releases(releases)
    latest = latest_stable(releases)
    github = GitHub()
    cleanup_previews(github, releases)
    failures, records = [], []
    for release in selected:
        version = release["version"]
        try:
            print(json.dumps(release, ensure_ascii=False), flush=True)
            files = discover_files(version, args.workers)
            print(f"{version}: {len(files)} files, {sum(f['size'] for f in files) / 1048576:.1f} MiB", flush=True)
            mirror(github, release, files, version == latest, args.workers)
            record = {"version": version, "status": "published"}
            if excluded := sorted(EXCLUDED_FILES.get(version, ())):
                record["excluded"] = excluded
                record["status"] = "published-with-exclusions"
            records.append(record)
        except Exception as exc:
            failures.append(version)
            status = ("upstream-unavailable" if isinstance(exc, UpstreamUnavailable) else
                      "integrity-failed" if isinstance(exc, IntegrityError) else "failed")
            detail = str(exc)
            for value in (SITE, CDN, urllib.parse.urlsplit(SITE).hostname, urllib.parse.urlsplit(CDN).hostname):
                if value:
                    detail = detail.replace(value, "[upstream]")
            records.append({"version": version, "status": status, "error": detail})
            print(f"ERROR {version} [{status}]: {detail}", flush=True)
    write_report(records)
    if failures:
        raise SystemExit(f"同步失败的版本: {', '.join(failures)}")


if __name__ == "__main__":
    main()
