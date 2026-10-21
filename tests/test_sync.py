"""Offline synchronization regressions; no GitHub calls or live-repo updates."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock
import uuid
import zipfile


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "sync_upstream.py"
SPEC = importlib.util.spec_from_file_location("sync_upstream", MODULE_PATH)
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr("repository-test/" + name, content)
    return stream.getvalue()


def upstream_files():
    abk = {name: ("upstream " + name).encode() for name in sync.ABK_FILES}
    abk.update({name: b"jobs: {}\n" for name in sync.ABK_SNAPSHOTS})
    abk.update({prefix + "example.txt": b"support" for prefix in sync.ABK_DIRECTORIES})
    abk["README.md"] = b"not imported"
    abk[".github/workflows/unwanted.yml"] = b"not imported"
    catalog = {name: b'{"versions": ["test"]}' for name in sync.CATALOG_FILES}
    catalog["LICENSE"] = b"catalog license"
    catalog["scripts/unwanted.sh"] = b"not imported"
    return abk, catalog


def inventory(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parents[2]
        self.temporary = self.fixture_parent / (".gki-sync-test-" + uuid.uuid4().hex)
        self.temporary.mkdir()
        self.addCleanup(self.cleanup_fixture)
        self.root = self.temporary / "repo"
        self.root.mkdir()
        self.lock = {
            "schema_version": 1,
            "abk": {"repository": "example/abk", "branch": "dev", "commit": "a" * 40},
            "catalog": {"repository": "example/catalog", "branch": "dev", "commit": "b" * 40},
        }
        self.abk, self.catalog = upstream_files()
        self.imports = sync.build_imports_from_zips(zipped(self.abk), zipped(self.catalog))
        for name, content in self.imports.items():
            self.write(name, content)
        self.write(sync.LOCK_PATH, json.dumps(self.lock).encode())
        self.write("README.md", b"my project")
        self.write("config/stock_defconfig", b"my device config")
        self.write("tools/generate_workflow.py", (
            "import argparse\nfrom pathlib import Path\n"
            "p = argparse.ArgumentParser(); p.add_argument('--root'); a = p.parse_args()\n"
            "r = Path(a.root)\n"
            "assert (r / 'config/stock_defconfig').read_text() == 'my device config'\n"
            f"o = r / {sync.WORKFLOW_PATH!r}\n"
            "o.parent.mkdir(parents=True, exist_ok=True)\n"
            "o.write_text('name: regenerated\\non: workflow_dispatch\\n')\n"
        ).encode())
        self.write(sync.WORKFLOW_PATH, b"name: initial\n")
        sync.record_current(self.root)

    def cleanup_fixture(self):
        target = self.temporary.resolve()
        if target.parent != self.fixture_parent.resolve() or not target.name.startswith(".gki-sync-test-"):
            raise AssertionError("Refusing to remove an unexpected fixture directory")
        shutil.rmtree(target)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_zip_selection_and_snapshot_relocation(self):
        self.assertNotIn("README.md", self.imports)
        self.assertNotIn(".github/workflows/unwanted.yml", self.imports)
        self.assertNotIn("scripts/unwanted.sh", self.imports)
        self.assertEqual(self.imports[".upstream/abk-build.yml"], b"jobs: {}\n")
        self.assertEqual(self.imports[sync.CATALOG_LICENSE], b"catalog license")

    def test_manifest_excludes_local_customizations_and_cannot_be_reset(self):
        files = sync.read_manifest(self.root)
        self.assertNotIn("config/stock_defconfig", files)
        self.assertNotIn(sync.WORKFLOW_PATH, files)
        self.assertNotIn("README.md", files)
        with self.assertRaisesRegex(sync.SyncError, "already exists"):
            sync.record_current(self.root)

    def test_update_rebuilds_in_staging_and_preserves_local_files(self):
        updated = dict(self.imports)
        updated["config/example.txt"] = b"updated support"
        updated["config/new-upstream.txt"] = b"new support"
        del updated["modules/example.txt"]
        new_lock = copy.deepcopy(self.lock)
        new_lock["abk"]["commit"] = "c" * 40
        result = sync.apply_imports(self.root, updated, new_lock)
        self.assertEqual(result, {"added": 1, "changed": 1, "removed": 1})
        self.assertFalse((self.root / "modules/example.txt").exists())
        self.assertEqual((self.root / "README.md").read_bytes(), b"my project")
        self.assertEqual((self.root / "config/stock_defconfig").read_bytes(), b"my device config")
        self.assertIn(b"regenerated", (self.root / sync.WORKFLOW_PATH).read_bytes())
        self.assertEqual(sync.read_lock(self.root)["abk"]["commit"], "c" * 40)
        self.assertEqual(sync.read_manifest(self.root)["config/new-upstream.txt"], sync._sha256(b"new support"))

    def test_modified_import_aborts_without_overwriting_anything(self):
        self.write("config/example.txt", b"intentional local customization")
        before = inventory(self.root)
        with self.assertRaisesRegex(sync.SyncError, "config/example.txt"):
            sync.apply_imports(self.root, self.imports, self.lock)
        self.assertEqual(before, inventory(self.root))

    def test_deleted_import_aborts_without_overwriting_anything(self):
        (self.root / "modules/example.txt").unlink()
        before = inventory(self.root)
        with self.assertRaisesRegex(sync.SyncError, "modules/example.txt"):
            sync.apply_imports(self.root, self.imports, self.lock)
        self.assertEqual(before, inventory(self.root))

    def test_new_upstream_file_cannot_replace_custom_stock_config(self):
        updated = {**self.imports, "config/stock_defconfig": b"upstream config"}
        before = inventory(self.root)
        with self.assertRaisesRegex(sync.SyncError, "stock_defconfig"):
            sync.apply_imports(self.root, updated, self.lock)
        self.assertEqual(before, inventory(self.root))

    def test_generator_failure_leaves_live_repository_unchanged(self):
        self.write("tools/generate_workflow.py", b"raise SystemExit('upstream schema changed')\n")
        before = inventory(self.root)
        updated = {**self.imports, "config/example.txt": b"new content"}
        with self.assertRaisesRegex(sync.SyncError, "incompatible"):
            sync.apply_imports(self.root, updated, self.lock)
        self.assertEqual(before, inventory(self.root))

    def test_missing_required_upstream_file_rejected(self):
        del self.abk[".github/workflows/build.yml"]
        with self.assertRaisesRegex(sync.SyncError, "layout changed"):
            sync.build_imports_from_zips(zipped(self.abk), zipped(self.catalog))

    def test_invalid_catalog_rejected(self):
        self.catalog[sync.CATALOG_FILES[0]] = b"not JSON"
        with self.assertRaisesRegex(sync.SyncError, "Invalid upstream version catalog"):
            sync.build_imports_from_zips(zipped(self.abk), zipped(self.catalog))

    def test_path_traversal_in_zip_rejected(self):
        self.abk["../config/escape.txt"] = b"bad"
        with self.assertRaisesRegex(sync.SyncError, "Unsafe ZIP member"):
            sync.build_imports_from_zips(zipped(self.abk), zipped(self.catalog))

    def test_case_colliding_zip_members_rejected(self):
        self.abk["config/Example.txt"] = b"case collision"
        with self.assertRaisesRegex(sync.SyncError, "case-colliding"):
            sync.build_imports_from_zips(zipped(self.abk), zipped(self.catalog))

    def test_manifest_cannot_delete_outside_whitelist(self):
        document = {"schema_version": 1, "files": {"README.md": sync._sha256(b"my project")}}
        self.write(sync.MANIFEST_PATH, json.dumps(document).encode())
        before = inventory(self.root)
        with self.assertRaisesRegex(sync.SyncError, "outside the import whitelist"):
            sync.apply_imports(self.root, self.imports, self.lock)
        self.assertEqual(before, inventory(self.root))

    def test_check_is_read_only(self):
        before = inventory(self.root)
        with mock.patch.object(sync, "resolve_commit", side_effect=["c" * 40, "b" * 40]) as resolve:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = sync.main(["--root", str(self.root), "--check"])
        self.assertEqual(result, 0)
        self.assertEqual(resolve.call_count, 2)
        self.assertIn("update available", output.getvalue())
        self.assertIn("locked:", output.getvalue())
        self.assertEqual(before, inventory(self.root))

    def test_offline_update_uses_locked_commits_and_never_networks(self):
        abk_zip = self.temporary / "abk.zip"
        catalog_zip = self.temporary / "catalog.zip"
        abk_zip.write_bytes(zipped(self.abk))
        catalog_zip.write_bytes(zipped(self.catalog))
        with mock.patch.object(sync, "_request", side_effect=AssertionError("offline update accessed network")):
            with contextlib.redirect_stdout(io.StringIO()):
                result = sync.main([
                    "--root", str(self.root), "--update", "--from-abk-zip", str(abk_zip),
                    "--from-catalog-zip", str(catalog_zip),
                ])
        self.assertEqual(result, 0)
        self.assertEqual(sync.read_lock(self.root), self.lock)

    def test_update_rejects_missing_manifest_before_network(self):
        (self.root / sync.MANIFEST_PATH).unlink()
        with mock.patch.object(sync, "_request", side_effect=AssertionError("network must not run")):
            with contextlib.redirect_stderr(io.StringIO()) as output:
                result = sync.main(["--root", str(self.root), "--update"])
        self.assertEqual(result, 1)
        self.assertIn("--record-current", output.getvalue())

    def test_io_failure_rolls_back_prior_replacements(self):
        self.write("config/aaa.txt", b"original")
        self.write("config/zzz.txt", b"original")
        real_write = sync._atomic_write
        failed = False

        def failing_write(path, content):
            nonlocal failed
            if path.name == "zzz.txt" and not failed:
                failed = True
                raise OSError("simulated disk error")
            return real_write(path, content)

        with mock.patch.object(sync, "_atomic_write", side_effect=failing_write):
            with self.assertRaisesRegex(sync.SyncError, "restored"):
                sync._commit_changes(self.root, {"config/aaa.txt": b"new", "config/zzz.txt": b"new"}, set())
        self.assertEqual((self.root / "config/aaa.txt").read_bytes(), b"original")
        self.assertEqual((self.root / "config/zzz.txt").read_bytes(), b"original")

    def test_api_auth_is_scoped_to_https_api_host_without_logging(self):
        fake_token = "offline-test-token"
        with mock.patch.dict(os.environ, {"GH_TOKEN": fake_token, "GITHUB_TOKEN": ""}):
            with mock.patch.object(sync.urllib.request, "build_opener") as builder:
                builder.return_value.open.return_value.__enter__.return_value.read.return_value = b"response"
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    with contextlib.redirect_stderr(io.StringIO()) as errors:
                        sync._request("https://api.github.com/repos/example/repo/commits/dev", accept="application/json")
                        api_request = builder.return_value.open.call_args.args[0]
                        self.assertEqual(api_request.get_header("Authorization"), "Bearer " + fake_token)
                        for url in (
                            "https://codeload.github.com/example/repo/zip/commit",
                            "https://api.github.com.example.com/path", "http://api.github.com/path",
                        ):
                            sync._request(url, accept="application/zip")
                            self.assertIsNone(builder.return_value.open.call_args.args[0].get_header("Authorization"))
        self.assertNotIn(fake_token, output.getvalue() + errors.getvalue())

    def test_api_authorization_is_removed_from_all_redirects(self):
        request = sync.urllib.request.Request(
            "https://api.github.com/example", headers={"Authorization": "Bearer offline-test-token", "Accept": "application/json"},
        )
        handler = sync._NoAuthRedirectHandler()
        for destination in ("https://codeload.github.com/archive", "https://api.github.com/redirected", "https://example.com/path"):
            redirected = handler.redirect_request(request, None, 302, "Found", {}, destination)
            self.assertIsNone(redirected.get_header("Authorization"))
            self.assertEqual(redirected.get_header("Accept"), "application/json")


if __name__ == "__main__":
    unittest.main()
