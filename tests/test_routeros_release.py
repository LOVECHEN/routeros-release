import hashlib
import json
import tempfile
import unittest
from html import escape
from pathlib import Path
from unittest.mock import patch

from scripts import routeros_release as mirror

mirror.SITE = "https://site.example.invalid"
mirror.CDN = "https://cdn.example.invalid/routeros"


def snapshot(name, data):
    return json.dumps({"memo": {"name": name}, "data": data})


def component(name, data):
    return '<div wire:snapshot="' + escape(snapshot(name, data), quote=True) + '"></div>'


def release(version="7.25.1", channel="stable", date="2026-11-03T10:22:55Z", archived=False):
    return {"version": version, "publishedAt": date, "archived": archived,
            "channels": {key: key == channel for key in mirror.CHANNELS}}


def file_item(content=b"official bytes", name="routeros-7.25.1.npk"):
    return {"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "url": f"{mirror.CDN}/7.25.1/{name}", "architecture": "x86", "key": "x86.main"}


def file_page(version="7.25.1", digest="a" * 64, filename="routeros-7.25.1.npk"):
    return mirror.Page(component("components.software.release-file-download", {
        "releaseFile": [{"name": filename, "size": 10, "checksumSHA256": digest,
                         "architecture": "x86", "key": "x86.main"}, {"s": "wrbl", "class": "File"}],
        "version": version,
    }))


class Response:
    def __init__(self, content, url="https://cdn.example.invalid/routeros/7.25.1/test.npk", headers=None):
        import io
        self.stream = io.BytesIO(content)
        self.url = url
        self.headers = headers or {}

    def read(self, size=-1):
        return self.stream.read(size)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class FakeGitHub:
    repo = "example/routeros-release"

    def __init__(self):
        self.current = None
        self.stored = {}
        self.uploaded = []
        self.published = []
        self.tags = []

    def release(self, tag):
        return self.current

    def dated_tag(self, tag, published):
        self.tags.append((tag, published))

    def api(self, path, data, method=None):
        if path == "releases":
            self.current = {"id": 1, **data}
            return self.current
        self.published.append(data)
        self.current.update(data)

    def assets(self, release_id):
        return dict(self.stored)

    def upload(self, release_id, path, existing=None):
        content = path.read_bytes()
        self.stored[path.name] = {"state": "uploaded", "size": len(content),
                                  "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        self.uploaded.append(path.name)


class DiscoveryTests(unittest.TestCase):
    def test_unpack_nested_livewire_arrays_and_wireable(self):
        value = [[[{"channels": [{"stable": True}, {"s": "arr"}]}, {"s": "arr"}]], {"s": "arr"}]
        self.assertEqual(mirror.unpack(value), [{"channels": {"stable": True}}])

    def test_page_parses_escaped_snapshot_and_architectures(self):
        page = file_page()
        page.feed('<button wire:click="selectArchitecture(\'arm64\')"></button>')
        self.assertEqual(page.files[0]["name"], "routeros-7.25.1.npk")
        self.assertEqual(page.architectures, {"arm64"})

    def test_select_latest_per_major_channel_by_date_not_listing_order(self):
        data = [release(), release("7.24.6", "longTerm", "2026-11-02T11:44:39Z"),
                release("6.49.23", date="2026-10-08T10:00:00Z"),
                release("7.25", date="2026-10-23T09:55:43Z"),
                release("7.26beta5", "development", "2026-11-02T12:24:12Z"),
                release("7.99", date="2026-11-04T00:00:00Z", archived=True)]
        selected = mirror.select_releases({r["version"]: r for r in data})
        self.assertEqual([r["version"] for r in selected], ["6.49.23", "7.24.6", "7.26beta5", "7.25.1"])

    def test_explicit_versions_deduplicated_and_validated(self):
        data = {"7.25.1": release()}
        self.assertEqual(len(mirror.select_releases(data, "7.25.1, 7.25.1")), 1)
        for value in ("7.25.2", "../../bad", "7.25;cmd", "8.0", ""):
            if value:
                with self.assertRaises(ValueError):
                    mirror.select_releases(data, value)

    def test_history_retains_final_releases_and_only_current_preview_heads(self):
        values = [release(), release("6.48.7", "longTerm", archived=True),
                  release("7.25rc3", "testing"), release("7.26beta5", "development"),
                  release("7.24rc4", "testing", "2026-08-01T00:00:00Z"),
                  release("7.24beta1", "development", "2026-05-01T00:00:00Z", archived=True)]
        history = {r["version"]: r for r in values}
        selected = mirror.select_releases(history, all_versions=True)
        self.assertEqual({r["version"] for r in selected}, {"7.25.1", "6.48.7", "7.25rc3", "7.26beta5"})
        with self.assertRaisesRegex(ValueError, "不再归档旧 beta/rc"):
            mirror.select_releases(history, "7.24rc4")

    def test_cleanup_plan_protects_current_previews_and_all_final_releases(self):
        from unittest.mock import Mock
        values = [release(), release("6.48.7", "longTerm", archived=True),
                  release("7.25rc3", "testing"), release("7.26beta5", "development"),
                  release("7.24rc4", "testing", "2026-08-01T00:00:00Z")]
        github = Mock()
        github.releases.return_value = [{"id": i, "tag_name": "routeros-" + r["version"], "draft": False}
                                       for i, r in enumerate(values)]
        plan = mirror.cleanup_releases(github, {r["version"]: r for r in values}, dry_run=True)
        self.assertEqual([r["version"] for r in plan["candidates"]], ["7.24rc4"])
        github.api.assert_not_called()

    def test_cleanup_deletes_only_safe_redundant_draft(self):
        from unittest.mock import Mock
        asset = {"name": "routeros-6.48.7.npk", "size": 10, "digest": "sha256:" + "a" * 64, "state": "uploaded"}
        draft = {"id": 1, "tag_name": "routeros-6.48.7", "draft": True, "assets": [asset]}
        published = {"id": 2, "tag_name": "routeros-6.48.7", "draft": False, "assets": [asset]}
        github = Mock()
        github.releases.return_value = [draft, published]
        history = {"6.48.7": release("6.48.7"), "7.25rc3": release("7.25rc3", "testing")}
        result = mirror.cleanup_releases(github, history)
        self.assertEqual([r["id"] for r in result["deleted"]], [1])
        github.api.assert_called_once_with("releases/1", method="DELETE")
        github.reset_mock()
        github.releases.return_value = [draft, {**published, "assets": []}]
        result = mirror.cleanup_releases(github, history)
        self.assertEqual(result["deleted"], [])
        github.api.assert_not_called()

    def test_cleanup_requires_current_preview_evidence(self):
        from unittest.mock import Mock
        with self.assertRaisesRegex(ValueError, "停止清理"):
            mirror.cleanup_releases(Mock(), {"7.25.1": release()})

    def test_missing_official_hash_fails_closed(self):
        with self.assertRaises(ValueError):
            mirror.normalize_files(file_page(digest=""), "7.25.1")

    def test_wrong_version_and_unsafe_filename_rejected(self):
        with self.assertRaises(ValueError):
            mirror.normalize_files(file_page(), "7.24")
        with self.assertRaises(ValueError):
            mirror.normalize_files(file_page(filename="../bad"), "7.25.1")

    def test_livewire_update_preserves_signed_snapshot_and_checks_version(self):
        name = mirror.COMPONENTS["routeros"]
        initial = component(name, {"version": "7.24.6"})
        initial += '<script data-csrf="token" data-update-uri="https://site.example.invalid/livewire/update"></script>'
        updated = snapshot(name, {"version": "7.25.1"})
        response = {"components": [{"snapshot": updated, "effects": {"html": ""}}]}
        with patch.object(mirror.HTTP, "text", return_value=initial), patch.object(
            mirror.HTTP, "open", return_value=Response(json.dumps(response).encode())
        ) as opener:
            page = mirror.load_page(version="7.25.1")
            payload = json.loads(opener.call_args.kwargs["data"])
            self.assertEqual(payload["components"][0]["updates"], {"version": "7.25.1"})
            self.assertEqual(page.component(name)["version"], "7.25.1")


class IntegrityTests(unittest.TestCase):
    def test_text_decodes_compressed_upstream_response(self):
        response = Response(mirror.gzip.compress(b"official html"), headers={"Content-Encoding": "gzip"})
        http = mirror.HTTP()
        with patch.object(http, "open", return_value=response):
            self.assertEqual(http.text("https://example.invalid"), "official html")

    def test_github_decodes_compressed_metadata_response(self):
        response = Response(mirror.gzip.compress(b'{"id":123}'), headers={"Content-Encoding": "gzip"})
        with patch.dict(mirror.os.environ, {"GITHUB_REPOSITORY": "example/routeros-release", "GH_TOKEN": "test"}):
            github = mirror.GitHub()
        with patch.object(github.http, "open", return_value=response) as opened:
            self.assertEqual(github.api("releases/123"), {"id": 123})
            self.assertEqual(opened.call_args.kwargs["headers"]["Accept-Encoding"], "gzip")

    def test_lookup_keeps_requested_tag_when_another_release_is_last(self):
        github = object.__new__(mirror.GitHub)
        wanted = {"id": 1, "tag_name": "routeros-7.25.1", "draft": False}
        last = {"id": 2, "tag_name": "routeros-6.48.7", "draft": True}
        with patch.object(github, "releases", return_value=[wanted, last]):
            self.assertEqual(github.release("routeros-7.25.1"), wanted)
            self.assertEqual(github.release("routeros-6.48.7"), last)

    def test_lookup_prefers_published_over_redundant_draft(self):
        github = object.__new__(mirror.GitHub)
        published = {"id": 1, "tag_name": "routeros-6.48.7", "draft": False}
        draft = {"id": 2, "tag_name": "routeros-6.48.7", "draft": True}
        with patch.object(github, "releases", return_value=[draft, published, published]):
            self.assertEqual(github.release("routeros-6.48.7"), published)

    def test_failed_index_build_never_leaves_partial_cache(self):
        github = object.__new__(mirror.GitHub)
        wanted = {"id": 1, "tag_name": "routeros-7.25.1", "draft": False}
        with patch.object(github, "releases", side_effect=[RuntimeError("network"), [wanted]]):
            with self.assertRaises(RuntimeError):
                github.release("routeros-7.25.1")
            self.assertIsNone(getattr(github, "_release_cache", None))
            self.assertEqual(github.release("routeros-7.25.1"), wanted)

    def test_conflicting_published_version_does_not_block_other_versions(self):
        github = object.__new__(mirror.GitHub)
        wanted = {"id": 1, "tag_name": "routeros-7.25.1", "draft": False}
        others = [{"id": i, "tag_name": "routeros-6.48.7", "draft": False} for i in (2, 3)]
        with patch.object(github, "releases", return_value=[wanted, *others]):
            self.assertEqual(github.release("routeros-7.25.1"), wanted)
            with self.assertRaises(ValueError):
                github.release("routeros-6.48.7")

    def test_pagination_deduplicates_repeated_release_ids(self):
        github = object.__new__(mirror.GitHub)
        first = [{"id": i} for i in range(100)]
        with patch.object(github, "api", side_effect=[first, [{"id": 99}, {"id": 100}]]):
            self.assertEqual(len(github.releases()), 101)

    def test_rate_limit_headers_and_backoff(self):
        with patch.object(mirror.time, "time", return_value=1000):
            self.assertEqual(mirror.throttle_delay({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4600"}), 3605)
        self.assertEqual(mirror.throttle_delay({"Retry-After": "120"}), 125)
        self.assertEqual(mirror.throttle_delay({}, "secondary rate limit", 2), 360)
        self.assertIsNone(mirror.throttle_delay({}, "not authorized"))

    def test_throttle_wait_does_not_consume_network_retry_budget(self):
        import urllib.error
        http = mirror.HTTP()
        error = lambda: urllib.error.HTTPError("https://example.invalid", 429, "limited", {"Retry-After": "1"}, None)
        with patch.object(http.opener, "open", side_effect=[*[error() for _ in range(5)], Response(b"ok")]), patch.object(mirror.time, "sleep"):
            with http.open("https://example.invalid") as response:
                self.assertEqual(response.read(), b"ok")

    def test_mutating_requests_are_paced(self):
        github = object.__new__(mirror.GitHub)
        with patch.object(mirror.time, "monotonic", return_value=100), patch.object(mirror.time, "sleep") as sleep:
            github.wait_for_write()
            github.wait_for_write()
        sleep.assert_called_once_with(8)

    def test_download_verified_against_size_and_hash(self):
        content = b"official bytes"
        with tempfile.TemporaryDirectory() as directory, patch.object(
            mirror.HTTP, "open", return_value=Response(content)
        ):
            path = mirror.download_verified(file_item(content), Path(directory))
            self.assertEqual(path.read_bytes(), content)

    def test_bad_download_is_removed_after_retries(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            mirror.HTTP, "open", side_effect=lambda *a, **kw: Response(b"corrupt")
        ), patch.object(mirror.time, "sleep"):
            with self.assertRaises(ValueError):
                mirror.download_verified(file_item(), Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_untrusted_redirect_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            mirror.HTTP, "open", side_effect=lambda *a, **kw: Response(b"official bytes", "https://other.example/file")
        ), patch.object(mirror.time, "sleep"):
            with self.assertRaises(ValueError):
                mirror.download_verified(file_item(), Path(directory))

    def test_size_alone_does_not_skip_unverified_asset(self):
        item = file_item()
        self.assertFalse(mirror.asset_matches({"size": item["size"], "state": "uploaded"}, item))
        self.assertFalse(mirror.asset_matches({"size": item["size"], "state": "starter",
                                               "digest": "sha256:" + item["sha256"]}, item))

    def test_assets_pagination_beyond_100(self):
        github = object.__new__(mirror.GitHub)
        first = [{"name": f"file-{i}"} for i in range(100)]
        with patch.object(github, "api", side_effect=[first, [{"name": "last"}]]) as api:
            self.assertEqual(len(github.assets(1)), 101)
            self.assertIn("page=2", api.call_args.args[0])

    def test_draft_lookup_uses_cached_paginated_releases(self):
        github = object.__new__(mirror.GitHub)
        draft = {"id": 123, "tag_name": "routeros-7.25.1", "draft": True}
        first = [{"id": i, "tag_name": f"unrelated-{i}"} for i in range(100)]
        with patch.object(github, "api", side_effect=[first, [draft]]) as api:
            self.assertEqual(github.release("routeros-7.25.1"), draft)
            self.assertIn("page=2", api.call_args.args[0])
            self.assertEqual(github.release("routeros-7.25.1"), draft)
            self.assertEqual(api.call_count, 2)

    def test_historical_catalog_reads_old_versions_and_skips_v5(self):
        source = '<div data-changelog-version="6.0"><b>Stable</b><span>2013-05-17</span></div>'
        source += '<div data-changelog-version="5.26"><span>2013-07-01</span></div>'
        result = mirror.historical_catalog(source)
        self.assertEqual(set(result), {"6.0"})
        self.assertEqual(result["6.0"]["publishedAt"], "2013-05-17T00:00:00Z")
        self.assertTrue(result["6.0"]["channels"]["stable"])

    def test_historical_catalog_requires_dates(self):
        with self.assertRaises(ValueError):
            mirror.historical_catalog('<div data-changelog-version="6.0">Stable</div>')


class PublishingTests(unittest.TestCase):
    def fake_download(self, item, directory):
        path = directory / item["name"]
        path.write_bytes(b"official bytes")
        return path

    def test_draft_first_then_publish_and_repeat_is_idempotent(self):
        github = FakeGitHub()
        files = [file_item()]
        with patch.object(mirror, "download_verified", side_effect=self.fake_download) as download:
            mirror.mirror(github, release(), files, True)
            self.assertFalse(github.current["draft"])
            self.assertEqual(github.current["make_latest"], "true")
            self.assertEqual(github.published[-1]["tag_name"], "routeros-7.25.1")
            self.assertEqual(github.uploaded, [files[0]["name"], "checksums.sha256"])
            self.assertEqual(github.tags[0][1], release()["publishedAt"])
            mirror.mirror(github, release(), files, True)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(len(github.uploaded), 2)

    def test_partial_release_repairs_only_missing_or_corrupt_files(self):
        github = FakeGitHub()
        github.current = {"id": 1, "draft": True}
        item = file_item()
        github.stored[item["name"]] = {"size": item["size"], "digest": "sha256:bad", "state": "uploaded"}
        with patch.object(mirror, "download_verified", side_effect=self.fake_download) as download:
            mirror.mirror(github, release(), [item], False)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(github.current["make_latest"], "false")

    def test_parallel_transfer_skips_verified_files(self):
        github = FakeGitHub()
        github.current = {"id": 1, "draft": True}
        files = [file_item(name=f"package-{i}.npk") for i in range(5)]
        github.stored[files[0]["name"]] = {"size": files[0]["size"], "state": "uploaded",
                                           "digest": "sha256:" + files[0]["sha256"]}
        with patch.object(mirror, "download_verified", side_effect=self.fake_download) as download:
            mirror.mirror(github, release(), files, False, workers=2)
        self.assertEqual(download.call_count, 4)
        self.assertNotIn(files[0]["name"], github.uploaded)
        self.assertEqual(len(github.stored), 6)

    def test_upload_retries_transient_command_failure(self):
        import subprocess
        github = object.__new__(mirror.GitHub)
        github.repo = "example/routeros-release"
        with patch.object(mirror.subprocess, "run", side_effect=[subprocess.CalledProcessError(1, "gh"), None]) as run, patch.object(mirror.time, "sleep"), patch.object(github, "assets", return_value={}):
            github.upload(123, Path("package.npk"))
            self.assertEqual(run.call_count, 2)
            self.assertIn("/releases/123/assets?", run.call_args.args[0][4])

    def test_upload_waits_for_reset_and_keeps_matching_asset_after_ambiguous_failure(self):
        import subprocess
        github = object.__new__(mirror.GitHub)
        github.repo = "example/routeros-release"
        content = b"official bytes"
        asset = {"state": "uploaded", "size": len(content), "digest": "sha256:" + hashlib.sha256(content).hexdigest()}
        error = subprocess.CalledProcessError(1, "gh", output="X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 4600\n", stderr="rate limit")
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror.time, "time", return_value=1000), patch.object(
            mirror.time, "sleep"
        ) as sleep, patch.object(mirror.subprocess, "run", side_effect=error) as run, patch.object(
            github, "assets", return_value={"package.npk": asset}
        ), patch.object(github, "api") as api:
            path = Path(directory) / "package.npk"
            path.write_bytes(content)
            github.upload(123, path)
        sleep.assert_any_call(3605)
        self.assertEqual(run.call_count, 1)
        api.assert_not_called()

    def test_report_distinguishes_upstream_integrity_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(mirror.os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            path = Path(directory) / "report.json"
            records = [{"version": "6.47beta19", "status": "integrity-failed", "error": "hash mismatch"}]
            mirror.write_report(records, path)
            self.assertEqual(json.loads(path.read_text())["versions"], records)

    def test_failed_download_does_not_publish_draft(self):
        github = FakeGitHub()
        with patch.object(mirror, "download_verified", side_effect=ValueError("bad hash")):
            with self.assertRaises(ValueError):
                mirror.mirror(github, release(), [file_item()], False)
            self.assertTrue(github.current["draft"])
            self.assertEqual(github.published, [])

    def test_corrupted_metadata_prevents_publication(self):
        github = FakeGitHub()
        upload = github.upload

        def corrupt(release_id, path, existing=None):
            upload(release_id, path, existing)
            if path.name == "checksums.sha256":
                github.stored[path.name]["digest"] = "sha256:bad"

        with patch.object(mirror, "download_verified", side_effect=self.fake_download), patch.object(
            github, "upload", side_effect=corrupt
        ):
            with self.assertRaisesRegex(ValueError, "checksums.sha256"):
                mirror.mirror(github, release(), [file_item()], False)
        self.assertTrue(github.current["draft"])

    def test_rc_is_prerelease(self):
        github = FakeGitHub()
        with patch.object(mirror, "download_verified", side_effect=self.fake_download):
            mirror.mirror(github, release("7.25rc3", "testing"), [file_item()], False)
        self.assertTrue(github.current["prerelease"])

    def test_metadata_contains_only_checksums(self):
        metadata = mirror.metadata_bytes(release(), [file_item()])
        self.assertEqual(metadata, mirror.metadata_bytes(release(), [file_item()]))
        self.assertEqual(set(metadata), {"checksums.sha256"})
        self.assertIn(file_item()["sha256"], metadata["checksums.sha256"].decode())

    def test_release_body_is_only_hash_first_table(self):
        item = file_item()
        body = mirror.release_notes(release(), [item])
        self.assertIn("### x86\n\n| SHA-256 | 文件 / File |", body)
        self.assertIn(f"| `{item['sha256']}` | [{item['name']}](https://github.com/", body)
        self.assertIn("/releases/download/routeros-7.25.1/", body)
        for text in ("MiB", "Channels", "Source", "Upstream", "# RouterOS", "sources.json"):
            self.assertNotIn(text, body)

    def test_architecture_groups(self):
        cases = {"routeros-7.25.1-arm64.npk": "arm64", "routeros-arm-6.48.7.npk": "arm",
                 "all_packages-mmips-7.1.zip": "mmips", "chr-7.25.1-arm64.img.zip": "chr-arm64",
                 "chr-7.25.1.vmdk.zip": "chr-x86_64", "routeros-x86-6.0.npk": "x86",
                 "routeros-7.25.1.npk": "x86", "netinstall64-7.25.1.zip": "tools",
                 "routeros-powerpc-6.0.npk": "ppc"}
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(mirror.architecture({"name": name}), expected)

    def test_channel_title_is_searchable(self):
        info = release()
        info["channels"]["longTerm"] = True
        self.assertEqual(mirror.release_title(info), "RouterOS 7.25.1 [longTerm stable]")

    def test_search_catalog_uses_inline_assets_and_excludes_drafts(self):
        from unittest.mock import Mock
        github = Mock()
        item = file_item()
        asset = {"name": item["name"], "digest": "sha256:" + item["sha256"]}
        published = {"id": 1, "tag_name": "routeros-7.25.1", "draft": False,
                     "created_at": release()["publishedAt"], "html_url": "https://github.com/example/release",
                     "assets": [asset]}
        github.releases.return_value = [published, {"id": 2, "tag_name": "routeros-7.99", "draft": True,
                                                   "created_at": release()["publishedAt"]}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            mirror.refresh_releases(github, {"7.25.1": release()}, output)
            data = json.loads((output / "catalog.json").read_text())
            self.assertEqual(len(data["releases"]), 1)
            self.assertEqual(data["releases"][0]["groups"][0]["arch"], "x86")
            self.assertEqual(data["releases"][0]["channels"], ["stable"])
            self.assertNotIn(mirror.CDN, (output / "catalog.json").read_text())
            self.assertIn('src="app.js?v=', (output / "index.html").read_text())
            self.assertIn('href="style.css?v=', (output / "index.html").read_text())
            github.assets.assert_not_called()
            draft_update = next(call for call in github.api.call_args_list if call.args[0] == "releases/2")
            self.assertIn("同步中", draft_update.args[1]["name"])
            self.assertIn("Incomplete mirror", draft_update.args[1]["body"])
            self.assertTrue(draft_update.args[1]["draft"])
            self.assertEqual(draft_update.args[1]["tag_name"], "routeros-7.99")

    def test_refresh_repairs_untagged_without_republishing_or_uploading(self):
        from unittest.mock import Mock
        github = Mock(repo="example/routeros-release")
        info = release("7.23.1", date="2026-06-03T07:49:31Z")
        github.releases.return_value = [{"id": 1, "tag_name": "untagged-abc", "name": "RouterOS 7.23.1",
            "draft": False, "created_at": "2026-11-03T15:37:46Z", "assets": [
                {"name": "routeros-7.23.1.npk", "digest": "sha256:" + "a" * 64}]}]
        with tempfile.TemporaryDirectory() as directory:
            mirror.refresh_releases(github, {"7.23.1": info, "7.25.1": release()}, Path(directory))
            record = json.loads((Path(directory) / "catalog.json").read_text())["releases"][0]
            self.assertEqual(record["date"], info["publishedAt"])
            self.assertIn("/routeros-7.23.1", record["url"])
            self.assertIn("/download/routeros-7.23.1/", record["groups"][0]["files"][0]["url"])
        github.dated_tag.assert_called_once_with("routeros-7.23.1", info["publishedAt"])
        payload = github.api.call_args.args[1]
        self.assertEqual(payload["tag_name"], "routeros-7.23.1")
        self.assertEqual(payload["make_latest"], "false")
        self.assertFalse(payload["draft"])
        github.upload.assert_not_called()

    def test_untagged_draft_resumes_by_canonical_version(self):
        github = object.__new__(mirror.GitHub)
        draft = {"id": 1, "tag_name": "untagged-abc", "name": "RouterOS 6.48.7 [longTerm] [同步中]", "draft": True}
        with patch.object(github, "releases", return_value=[draft]):
            self.assertEqual(github.release("routeros-6.48.7"), draft)

    def test_dated_tag_uses_current_main_tree_not_stale_workflow_commit(self):
        import urllib.error
        github = object.__new__(mirror.GitHub)
        responses = [urllib.error.HTTPError("https://example.invalid", 404, "missing", {}, None),
                     {"sha": "current-main"}, {"tree": {"sha": "current-tree"}},
                     {"sha": "dated-archive"}, {}]
        with patch.dict(mirror.os.environ, {"GITHUB_SHA": "stale-workflow"}), patch.object(
            github, "api", side_effect=responses
        ) as api:
            github.dated_tag("routeros-7.23.1", "2026-06-03T07:49:31Z")
        commit = api.call_args_list[3].args[1]
        self.assertEqual(commit["tree"], "current-tree")
        self.assertEqual(commit["parents"], ["current-main"])
        self.assertEqual(commit["committer"]["date"], "2026-06-03T07:49:31Z")

    def test_unrelated_untagged_release_is_not_claimed(self):
        self.assertIsNone(mirror.release_version({"tag_name": "untagged-abc", "name": "other 7.23.1"}))
        self.assertIsNone(mirror.release_version({"tag_name": "untagged-abc", "name": "RouterOS 7.23.1 suffix"}))

    def test_select_all_versions_including_archived(self):
        values = [release(), release("7.1", archived=True)]
        selected = mirror.select_releases({r["version"]: r for r in values}, all_versions=True)
        self.assertEqual(len(selected), 2)

    def test_read_only_catalog_never_changes_releases(self):
        from unittest.mock import Mock
        github = Mock()
        github.releases.return_value = [{"id": 1, "tag_name": "routeros-7.25.1", "draft": False,
            "created_at": release()["publishedAt"], "html_url": "https://github.com/example/release",
            "assets": [{"name": file_item()["name"], "digest": "sha256:" + file_item()["sha256"]},
                       {"id": 2, "name": "sources.json"}]},
            {"id": 3, "tag_name": "routeros-7.25", "draft": True, "created_at": release()["publishedAt"]}]
        with tempfile.TemporaryDirectory() as directory:
            mirror.refresh_releases(github, {"7.25.1": release()}, Path(directory), read_only=True)
            self.assertEqual(len(json.loads((Path(directory) / "catalog.json").read_text())["releases"]), 1)
        github.api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
