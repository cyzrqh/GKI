import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import release_notes as notes

UTC = datetime(2026, 10, 22, 6, 7, 8, tzinfo=timezone.utc)
MANAGER = {
    "file": "SukiSU-Manager-17ff94d-arm64.apk", "package": "com.cyzrqh.gki.sukisu",
    "ksu_variant": "SukiSU", "ksu_branch": "Dev(开发)", "ksu_ref": "17ff94d" + "a" * 33,
    "ksu_version_code": 40321, "ksu_version_name": "v4.0.2-17ff94d", "susfs_version": "v2.3.0",
    "built_at_utc": "2026-10-22T05:30:00+00:00",
}
KERNEL = {
    "file": "android14-6.1.145-2025-08-AnyKernel3.zip", "android_version": "android14",
    "kernel_version": "6.1", "sub_level": "145", "os_patch_level": "2025-08",
    "ksu_variant": "SukiSU", "ksu_branch": "Dev(开发)", "ksu_ref": MANAGER["ksu_ref"],
    "ksu_version_code": 40321, "ksu_version_name": "v4.0.2-17ff94d", "susfs_version": "v2.3.0",
    "features": {"susfs": True, "hook": "inline", "mount": None, "lto": "thin", "bbg": True,
                 "zram": False, "lz4kd": False, "kpm": True, "re_kernel": False,
                 "ddk": False, "ntsync": True, "network": True, "virtualization": "off",
                 "custom_modules": "drivers/example", "kernel_config": "CONFIG_EXAMPLE=y",
                 "zram_algorithm": "zstd"},
    "built_at_utc": "2026-10-22T06:00:00+00:00",
    "config_source": "Image / IKCONFIG",
}


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "tests" / (".release-notes-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, filename, document):
        path = self.root / filename
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def render(self, manager=MANAGER, kernels=None):
        return notes.render_notes(manager, [KERNEL] if kernels is None else kernels, generated_at=UTC)

    def test_actual_versions_time_and_all_selected_feature_states(self):
        body = self.render()
        for expected in ("# GKI Kernel Release", "| 发布类型 | Release |", "2026-10-22 06:07:08 UTC",
                         "v4.0.2-17ff94d（版本号 40321）", "v2.3.0", "com.cyzrqh.gki.sukisu",
                         "android14 / 6.1.145 / 2025-08", "| SUSFS | 启用（v2.3.0） |",
                         "| Hook 方式 | inline |", "| 挂载方式 | 未知 |", "| LTO | thin |",
                         "| BBG 安全补丁 | 启用 |", "| ZRAM | 关闭 |", "| LZ4KD | 关闭 |",
                         "| KPM | 启用 |", "| Re-Kernel | 关闭 |", "| DDK | 关闭 |",
                         "| NTSync | 启用 |", "| 网络增强 | 启用 |", "| 虚拟化 | 关闭 |",
                         "drivers/example", "CONFIG\\_EXAMPLE=y", "zram\\_algorithm", "zstd",
                         MANAGER["file"], KERNEL["file"], MANAGER["built_at_utc"], KERNEL["built_at_utc"]):
            with self.subTest(expected=expected):
                self.assertIn(expected, body)
        self.assertNotIn("MagicMount", body)
        self.assertNotIn("Magic Mount", body)

    def test_multiple_kernel_targets_keep_their_actual_values(self):
        second = copy.deepcopy(KERNEL)
        second.update(android_version="android15", kernel_version="6.6", sub_level=99,
                      os_patch_level="2026-01", file="android15-6.6.99-2026-01-AnyKernel3.zip")
        second["features"].update(mount="overlayfs", hook="manual", kpm=False)
        body = self.render(kernels=[KERNEL, second])
        self.assertIn("本次包含 2 个内核目标", body)
        self.assertNotIn("## 内核", body)
        self.assertEqual(body.count("## 构建配置\n"), 1)
        self.assertEqual(body.count("| KernelSU 变体 |"), 1)
        self.assertEqual(body.count("| LTO | thin |"), 1)
        self.assertIn("| 构建范围 | Android 14 / 6.1、Android 15 / 6.6 |", body)
        self.assertIn("| 实际内核目标数 | 2 |", body)
        self.assertIn("| 配置来源 | Image / IKCONFIG |", body)
        self.assertIn("| 挂载方式 | 未知（Android 14 / 6.1）；overlayfs（Android 15 / 6.6） |", body)
        self.assertIn("| Hook 方式 | inline（Android 14 / 6.1）；manual（Android 15 / 6.6） |", body)
        self.assertIn("| KPM | 启用（Android 14 / 6.1）；关闭（Android 15 / 6.6） |", body)

    def test_missing_values_are_unknown_without_inventing_defaults(self):
        body = self.render(manager={}, kernels=[{}])
        for label in ("KernelSU 变体", "KernelSU 分支", "SUSFS", "挂载方式", "Hook 方式", "BBG 安全补丁"):
            self.assertIn(f"| {label} | 未知 |", body)
        self.assertIn("未知 / 未知 / 未知", body)
        self.assertNotIn("v2.3.0", body)
        self.assertNotIn("Magic", body)

    def test_proven_mount_value_is_reported_only_when_supplied(self):
        kernel = copy.deepcopy(KERNEL)
        kernel["features"]["mount"] = "Magic Mount"
        self.assertIn("| 挂载方式 | Magic Mount |", self.render(kernels=[kernel]))

    def test_apk_only_and_kernel_only_metadata_are_supported(self):
        apk_only = self.render(kernels=[])
        self.assertIn("本次包含 0 个内核目标", apk_only)
        self.assertIn("本次仅发布管理器安装包", apk_only)
        self.assertIn("无内核（仅管理器 APK）", apk_only)
        self.assertNotIn("## 构建配置", apk_only)
        self.assertNotIn("| SUSFS |", apk_only)
        kernel_only = self.render(manager=None)
        self.assertIn("| KernelSU 变体 | SukiSU |", kernel_only)
        self.assertIn("| 管理器 APK | 未知 |", kernel_only)
        vanilla = copy.deepcopy(KERNEL)
        vanilla["ksu_variant"] = "None"
        self.assertIn("| KernelSU 变体 | None |", self.render(manager=None, kernels=[vanilla]))

    def test_actions_report_has_truthful_location_and_time_without_changing_configuration(self):
        release = self.render()
        actions = notes.render_notes(MANAGER, [KERNEL], generated_at=UTC, report_kind='actions')
        self.assertTrue(actions.startswith('# GKI 构建结果\n'))
        self.assertIn('| 输出位置 | Actions 构建产物 |', actions)
        self.assertIn('| 记录时间 | 2026-10-22 06:07:08 UTC |', actions)
        self.assertNotIn('| 发布类型 |', actions)
        self.assertNotIn('| 发布时间 |', actions)
        self.assertNotIn('GKI Kernel Release', actions)
        self.assertEqual(release.split('## 构建配置\n', 1)[1], actions.split('## 构建配置\n', 1)[1])
        self.assertEqual(actions.count('## 构建配置\n'), 1)
        self.assertIn(MANAGER['file'], actions)
        self.assertIn(KERNEL['file'], actions)
        self.assertEqual(release, notes.render_notes(MANAGER, [KERNEL], generated_at=UTC, report_kind='release'))

    def test_actions_apk_only_report_says_built_not_published(self):
        body = notes.render_notes(MANAGER, [], generated_at=UTC, report_kind='actions')
        self.assertIn('本次仅构建管理器安装包，没有内核构建。', body)
        self.assertIn('内核功能配置不适用于本次 APK 构建。', body)
        self.assertIn('| 输出位置 | Actions 构建产物 |', body)
        self.assertNotIn('发布', body)
        self.assertNotIn('## 构建配置', body)
        self.assertIn(MANAGER['file'], body)

    def test_report_kind_validation_and_file_generation(self):
        manager = self.write('manager.json', MANAGER)
        output = self.root / 'actions.md'
        notes.generate_notes(manager, [], output, report_kind='actions')
        self.assertIn('# GKI 构建结果', output.read_text(encoding='utf-8'))
        for value in ('', 'published', None, True, []):
            with self.subTest(report_kind=value), self.assertRaisesRegex(notes.ReleaseNotesError, 'Report kind'):
                notes.render_notes(MANAGER, [KERNEL], report_kind=value)

    def test_mixed_kernel_versions_are_not_reported_as_one_shared_version(self):
        other = copy.deepcopy(KERNEL)
        other["ksu_version_code"] = 50000
        body = self.render(manager=None, kernels=[KERNEL, other])
        self.assertNotIn("多个值", body)
        self.assertIn("版本号 40321", body)
        self.assertIn("版本号 50000", body)
        self.assertIn("目标 1", body)
        self.assertIn("目标 2", body)

    def test_mixed_lto_and_lz4kd_are_grouped_in_one_table_without_losing_disabled_states(self):
        kernels = []
        for android, series, sublevel in (("android13", "5.15", "194"),
                                         ("android14", "6.1", "145"),
                                         ("android15", "6.6", "143"),
                                         ("android16", "6.12", "92")):
            kernel = copy.deepcopy(KERNEL)
            kernel.update(android_version=android, kernel_version=series, sub_level=sublevel,
                          file=f"{android}-{series}.{sublevel}-AnyKernel3.zip")
            kernel["features"].update(lto="none" if series == "6.12" else "thin",
                                      lz4kd=series != "6.12")
            kernels.append(kernel)
        body = self.render(kernels=kernels)
        labels = "Android 13 / 5.15、Android 14 / 6.1、Android 15 / 6.6"
        self.assertIn(f"| LTO | thin（{labels}）；none（Android 16 / 6.12） |", body)
        self.assertIn(f"| LZ4KD | 启用（{labels}）；关闭（Android 16 / 6.12） |", body)
        self.assertEqual(body.count("| SUSFS | 启用（v2.3.0） |"), 1)
        self.assertEqual(body.count("| Re-Kernel | 关闭 |"), 1)
        self.assertEqual(body.count("## 构建配置\n"), 1)
        self.assertNotIn("## 内核", body)

    def test_same_series_conflicting_versions_use_full_targets_only_for_conflicting_rows(self):
        other = copy.deepcopy(KERNEL)
        other.update(sub_level="146", os_patch_level="2025-09", file="android14-6.1.146-2025-09-AnyKernel3.zip")
        other["features"].update(lto="full", lz4kd=True)
        body = self.render(kernels=[KERNEL, other])
        self.assertIn("| 构建范围 | Android 14 / 6.1 |", body)
        self.assertIn("| LTO | thin（android14 / 6.1.145 / 2025-08）；full（android14 / 6.1.146 / 2025-09） |", body)
        self.assertIn("| LZ4KD | 关闭（android14 / 6.1.145 / 2025-08）；启用（android14 / 6.1.146 / 2025-09） |", body)
        self.assertIn("| Hook 方式 | inline |", body)
        self.assertEqual(body.count("| 配置来源 | Image / IKCONFIG |"), 1)

    def test_same_series_same_version_different_months_are_disambiguated_when_configs_differ(self):
        other = copy.deepcopy(KERNEL)
        other.update(os_patch_level="2025-09", file="android14-6.1.145-2025-09-AnyKernel3.zip")
        other["features"].update(lz4kd=True)
        body = self.render(kernels=[KERNEL, other])
        self.assertIn("| LTO | thin |", body)
        self.assertIn("| LZ4KD | 关闭（android14 / 6.1.145 / 2025-08）；启用（android14 / 6.1.145 / 2025-09） |", body)
        self.assertIn("2025-09-AnyKernel3.zip", body)

    def test_optional_unknown_features_are_omitted_but_known_disabled_values_are_kept(self):
        other = copy.deepcopy(KERNEL)
        other["features"] = {"susfs": True, "kpm": False, "re_kernel": False, "ddk": None,
                             "custom_optional": None}
        body = self.render(kernels=[other])
        self.assertIn("| Re-Kernel | 关闭 |", body)
        self.assertIn("| KPM | 关闭 |", body)
        self.assertNotIn("| DDK |", body)
        self.assertNotIn("custom\\_optional", body)
        self.assertIn("| Hook 方式 | 未知 |", body)

    def test_unknown_config_for_one_series_is_not_hidden_by_other_series(self):
        other = copy.deepcopy(KERNEL)
        other.update(android_version="android16", kernel_version="6.12", sub_level="92")
        other["features"].pop("lz4kd")
        body = self.render(kernels=[KERNEL, other])
        self.assertIn("| LZ4KD | 关闭（Android 14 / 6.1）；未知（Android 16 / 6.12） |", body)

    def test_dispatch_aliases_and_boolean_strings_are_supported(self):
        kernel = {"variant": "Official", "branch": "Custom", "target": "custom-target",
                  "enable_susfs": "false", "use_zram": "true", "use_kpm": "off",
                  "features": {"hook_method": "kprobe", "lto_mode": "full", "use_bbg": True}}
        body = self.render(kernels=[kernel])
        for expected in ("| SUSFS | 关闭 |", "| ZRAM | 启用 |", "| KPM | 关闭 |",
                         "| Hook 方式 | kprobe |", "| LTO | full |", "| BBG 安全补丁 | 启用 |"):
            self.assertIn(expected, body)

    def test_markdown_html_and_newline_injection_are_escaped_in_names_and_values(self):
        kernel = copy.deepcopy(KERNEL)
        payload = "x|**bold**\r\n# heading<script>alert(1)</script>[link](https://example.test)"
        kernel["target"] = payload
        kernel["ksu_version_name"] = payload
        kernel["features"][payload] = payload
        body = self.render(kernels=[kernel])
        self.assertNotIn("<script>", body)
        self.assertNotIn("\n# heading", body)
        self.assertNotIn("[link](", body)
        self.assertIn("x\\|\\*\\*bold\\*\\* ⏎ \\# heading&lt;script&gt;", body)
        self.assertNotIn("\r", body)

    def test_private_feature_fields_are_omitted(self):
        kernel = copy.deepcopy(KERNEL)
        kernel["features"].update(kpm_password="private-value", signing_token="private-value",
                                   private_key="private-value")
        self.assertNotIn("private-value", self.render(kernels=[kernel]))

    def test_filenames_reject_paths_controls_and_unexpected_asset_types(self):
        for filename in ("../manager.apk", "dir/manager.apk", "dir\\manager.apk", "manager.apk\n# injected",
                         "manager.apk|link", "<manager>.apk", "manager.zip", "-manager.apk",
                         "manager..apk", "manager.apk ", "C:manager.apk"):
            manager = {**MANAGER, "file": filename}
            with self.subTest(filename=filename), self.assertRaises(notes.ReleaseNotesError):
                self.render(manager=manager)
        with self.assertRaises(notes.ReleaseNotesError):
            self.render(kernels=[{**KERNEL, "file": "unexpected.apk"}])

    def test_multiple_kernel_files_accept_images_and_reject_duplicates(self):
        kernel = copy.deepcopy(KERNEL)
        kernel["files"] = [kernel["file"], "boot.img"]
        self.assertIn("boot.img", self.render(kernels=[kernel]))
        for changes in ({"files": ["boot.img", "boot.img"], "file": "boot.img"},
                        {"files": "boot.img"}, {"files": ["boot.img"]},
                        {"filename": "other.zip"}):
            with self.subTest(changes=changes), self.assertRaises(notes.ReleaseNotesError):
                self.render(kernels=[{**KERNEL, **changes}])

    def test_nested_feature_values_and_nonfinite_numbers_are_rejected(self):
        for value in ({"untrusted": "nested"}, ["nested"], float("nan"), float("inf")):
            kernel = copy.deepcopy(KERNEL)
            kernel["features"]["custom"] = value
            with self.subTest(value=value), self.assertRaises(notes.ReleaseNotesError):
                self.render(kernels=[kernel])
        path = self.root / "invalid.json"
        for text in ("[]", "not JSON", '{"value":NaN}'):
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(notes.ReleaseNotesError):
                notes.load_metadata(path)

    def test_time_is_converted_to_utc_and_naive_time_is_rejected(self):
        local = UTC.astimezone(timezone(timedelta(hours=8)))
        body = notes.render_notes(MANAGER, [], generated_at=local)
        self.assertIn("2026-10-22 06:07:08 UTC", body)
        with self.assertRaises(notes.ReleaseNotesError):
            notes.render_notes(MANAGER, [], generated_at=datetime(2026, 10, 22))

    def test_cli_writes_selected_metadata_and_accepts_optional_manager(self):
        manager = self.write("manager.json", MANAGER)
        kernel = self.write("kernel.json", KERNEL)
        output = self.root / "out/RELEASE.md"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(notes.main(["--manager", str(manager), "--kernels", str(kernel),
                                         "--output", str(output)]), 0)
        self.assertIn("v4.0.2-17ff94d", output.read_text(encoding="utf-8"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(notes.main(["--kernels", str(kernel), "--output", str(output)]), 0)
        self.assertIn("| 管理器 APK | 未知 |", output.read_text(encoding="utf-8"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(notes.main(["--manager", str(manager), "--kernels", "--output", str(output)]), 0)
        self.assertIn("本次仅发布管理器安装包", output.read_text(encoding="utf-8"))

    def test_invalid_metadata_does_not_overwrite_existing_output(self):
        manager = self.write("bad.json", {**MANAGER, "file": "../bad.apk"})
        output = self.root / "RELEASE.md"
        output.write_text("previous release", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(notes.main(["--manager", str(manager), "--output", str(output)]), 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous release")
        self.assertIn("invalid release asset filename", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
