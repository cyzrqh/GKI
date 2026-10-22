import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pin_build_dependencies as pin

KSU = "c" * 40
SUSFS = "a" * 40
PROFILE = {"schema_version": 1, "sukisu_susfs": {
    "ksu_commits": [KSU], "tiers": ["Stable(标准)", "Dev(开发)"],
    "version": "v2.2.0", "pins": {"android14-6.1": SUSFS}}}


class BuildDependencyTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".dependencies-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.profile = self.root / "build-compatibility.json"
        self.write_profile(PROFILE)
        self.repo = self.root / "susfs4ksu"
        (self.repo / ".git").mkdir(parents=True)
        self.header = self.repo / "kernel_patches/include/linux/susfs.h"
        self.header.parent.mkdir(parents=True)
        self.header.write_text('#define SUSFS_VERSION "v2.2.0"\n', encoding="utf-8")
        self.output = self.root / "environment.txt"
        self.env = patch.dict(os.environ, {"GITHUB_ENV": str(self.output)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_profile(self, document):
        self.profile.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def select(self, **overrides):
        values = dict(root=self.root, variant="SukiSU", ksu_ref=KSU, tier="Dev(开发)", target="android14-6.1")
        values.update(overrides)
        return pin.selected_pin(**values)

    def run_pin(self, **overrides):
        values = dict(root=self.root, susfs_dir=self.repo, variant="SukiSU", ksu_ref=KSU,
                      tier="Dev(开发)", target="android14-6.1")
        values.update(overrides)
        return pin.pin_dependencies(**values)

    def mock_git(self, args, **kwargs):
        operation = args[3]
        output = SUSFS if operation == "rev-parse" else "2026-06-22 10:00:03 +0800" if operation == "log" else ""
        return subprocess.CompletedProcess(args, 0, stdout=output + "\n", stderr="")

    def test_matches_only_exact_known_sukisu_tiers_and_commit(self):
        self.assertEqual(self.select(), (SUSFS, "v2.2.0"))
        self.assertEqual(self.select(ksu_ref=KSU.upper(), tier="Stable(标准)"), (SUSFS, "v2.2.0"))
        for values in ({"variant": "Official"}, {"variant": "BakaSU"}, {"tier": "Latest(最新)"},
                       {"tier": "Custom(自定义)"}, {"ksu_ref": "d" * 40}):
            with self.subTest(values=values), patch.object(pin.subprocess, "run") as run:
                self.assertFalse(self.run_pin(**values))
                run.assert_not_called()

    def test_explicit_custom_setting_wins(self):
        config = self.root / "config/config"
        config.parent.mkdir()
        for text, expected in (("custom=true\n", True), ("# custom=true\ncustom=false\n", False),
                               (" custom = TRUE # manual\n", True)):
            config.write_text(text, encoding="utf-8")
            self.assertEqual(pin.custom_configuration(self.root), expected)
            if expected:
                with patch.object(pin.subprocess, "run") as run:
                    self.assertFalse(self.run_pin())
                    run.assert_not_called()

    def test_checkout_verifies_full_sha_and_version_then_updates_environment(self):
        with patch.object(pin.subprocess, "run", side_effect=self.mock_git) as run:
            self.assertTrue(self.run_pin())
        operations = [call.args[0][3:] for call in run.call_args_list]
        self.assertEqual(operations[:3], [["fetch", "--depth=1", "origin", SUSFS],
                                          ["checkout", "--detach", SUSFS], ["rev-parse", "HEAD"]])
        for call in run.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertNotIn("shell", call.kwargs)
            self.assertEqual(call.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         f"SUSFS_LATEST_COMMIT_DATE=2026-06-22 10:00:03 +0800\nPINNED_SUSFS_SHA={SUSFS}\nSUSFS_COMPAT_VERSION=v2.2.0\n")

    def test_mismatched_head_stops_without_environment_outputs(self):
        def fake(args, **kwargs):
            result = self.mock_git(args, **kwargs)
            if args[3] == "rev-parse":
                result.stdout = "b" * 40
            return result
        with patch.object(pin.subprocess, "run", side_effect=fake):
            with self.assertRaisesRegex(pin.CompatibilityError, "does not match"):
                self.run_pin()
        self.assertFalse(self.output.exists())

    def test_mismatched_version_stops_without_environment_outputs(self):
        self.header.write_text('#define SUSFS_VERSION "v2.3.0"\n', encoding="utf-8")
        with patch.object(pin.subprocess, "run", side_effect=self.mock_git):
            with self.assertRaisesRegex(pin.CompatibilityError, "must declare v2.2.0"):
                self.run_pin()
        self.assertFalse(self.output.exists())

    def test_failed_fetch_is_sanitized_and_stops_checkout(self):
        failure = subprocess.CompletedProcess([], 1, stdout="", stderr="secret-token-from-remote")
        with patch.object(pin.subprocess, "run", return_value=failure) as run:
            with self.assertRaisesRegex(pin.CompatibilityError, "fetch failed") as error:
                self.run_pin()
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_profile_rejects_invalid_fields_and_short_shas(self):
        mutations = [lambda d: d.update(schema_version=2),
                     lambda d: d["sukisu_susfs"].update(ksu_commits=["abc"]),
                     lambda d: d["sukisu_susfs"].update(tiers=["Latest(最新)"]),
                     lambda d: d["sukisu_susfs"].update(version="v2.2.0\n"),
                     lambda d: d["sukisu_susfs"].update(pins={"../target": SUSFS}),
                     lambda d: d["sukisu_susfs"].update(pins={"android14-6.1": SUSFS + "\n"})]
        for mutate in mutations:
            document = copy.deepcopy(PROFILE)
            mutate(document)
            self.write_profile(document)
            with self.subTest(document=document):
                with self.assertRaises(pin.CompatibilityError):
                    self.select()

    def test_missing_target_and_bad_target_are_rejected(self):
        with self.assertRaisesRegex(pin.CompatibilityError, "No compatible SUSFS pin"):
            self.select(target="android15-6.6")
        with self.assertRaisesRegex(pin.CompatibilityError, "Invalid Android/kernel target"):
            self.select(target="android14-6.1;echo bad")

    def test_refuses_workspace_root_or_uncloned_or_external_directory(self):
        for path in (self.root, self.root / "uncloned", self.root.parent):
            with self.subTest(path=path), patch.object(pin.subprocess, "run") as run:
                with self.assertRaisesRegex(pin.CompatibilityError, "separate cloned repository"):
                    self.run_pin(susfs_dir=path)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
