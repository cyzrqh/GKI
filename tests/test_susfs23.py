import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import apply_susfs23 as susfs


@unittest.skipUnless(shutil.which("git"), "Git is required")
class Susfs23ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "tests" / f".susfs23-{uuid.uuid4().hex}"
        self.addCleanup(self.cleanup_directory)
        self.repository = self.directory / "kernel-root/KernelSU"
        self.repository.mkdir(parents=True)
        self.run_git("init", "--quiet")
        self.run_git("config", "core.autocrlf", "false")
        self.run_git("config", "user.name", "SUSFS integration test")
        self.run_git("config", "user.email", "test@example.invalid")
        (self.repository / "kernel/core").mkdir(parents=True)
        self.build = self.repository / "kernel/Kbuild"
        self.core = self.repository / "kernel/core/init.c"
        self.build.write_text("kernelsu-objs := core/init.o\n", encoding="utf-8", newline="\n")
        self.core.write_text("/* original kernel init */\n", encoding="utf-8", newline="\n")
        self.providers = []
        for arch in susfs.PATCH_MEMORY_ARCHES:
            provider = self.repository / f"kernel/hook/{arch}/patch_memory.c"
            provider.parent.mkdir(parents=True)
            provider.write_text("int ksu_patch_text(void *dst, void *src, int len, int flags)\n"
                                "{ return stop_machine(patch_callback, dst, cpu_online_mask); }\n",
                                encoding="utf-8", newline="\n")
            self.providers.append(provider)
        self.uapi = self.repository / "uapi/ioctl.h"
        self.uapi.parent.mkdir()
        self.uapi.write_text("/* reviewed UAPI */\n", encoding="utf-8", newline="\n")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "test source")
        self.head = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.profile = {
            "ksu_commits": [self.head],
            "reviewed_kernel_tree": self.run_git("rev-parse", "HEAD:kernel").stdout.strip(),
            "reviewed_uapi_tree": self.run_git("rev-parse", "HEAD:uapi").stdout.strip(),
        }
        self.susfs = self.directory / "susfs/kernel_patches/include/linux"
        self.susfs.mkdir(parents=True)
        self.header = self.susfs / "susfs.h"
        self.header.write_text('#define SUSFS_VERSION "v2.3.0"\n', encoding="utf-8")
        self.core.write_text("/* integrated kernel init */\n", encoding="utf-8", newline="\n")
        self.build.write_text("kernelsu-objs := core/init.o preinit/preinit.o\n", encoding="utf-8", newline="\n")
        self.patch = self.directory / "fixture.patch"
        self.patch.write_text(self.run_git("diff", "--full-index").stdout, encoding="utf-8", newline="\n")
        self.run_git("restore", ".")

    def cleanup_directory(self):
        target = self.directory.resolve()
        if target.parent != (ROOT / "tests").resolve() or not target.name.startswith(".susfs23-"):
            raise ValueError("Unexpected test cleanup target")
        def remove_readonly(function, name, error):
            Path(name).chmod(stat.S_IWRITE)
            function(name)
        shutil.rmtree(target, onexc=remove_readonly)

    def run_git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.repository), *arguments], check=True,
                              capture_output=True, text=True, encoding="utf-8")

    def state(self):
        return {str(path.relative_to(self.repository)): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                                       path.stat().st_mtime_ns)
                for path in (self.build, self.core)}

    def apply_fixture(self):
        # Exercise Git's real atomic apply/check/reverse behavior on a small repo.
        # The complete patch and official reviewed trees are verified separately.
        with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                mock.patch.object(susfs, "verify_integration") as verify:
            result = susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
            verify.assert_called_once_with(self.repository.resolve())
            return result

    def test_complete_application_and_idempotent_reverification(self):
        self.assertTrue(self.apply_fixture())
        self.assertIn("integrated", self.core.read_text())
        self.assertIn("preinit/preinit.o", self.build.read_text())
        applied = self.state()
        self.assertFalse(self.apply_fixture())
        self.assertEqual(applied, self.state())

    def test_unreviewed_sukisu_tree_refused_before_modification(self):
        before = self.state()
        strict_profile = {**self.profile, 'reviewed_kernel_tree': 'a' * 40}
        with mock.patch.object(susfs, 'load_profile', return_value=strict_profile), \
                self.assertRaisesRegex(ValueError, "requires the reviewed SukiSU kernel tree"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_new_manager_commit_with_identical_kernel_and_uapi_is_accepted(self):
        manager = self.repository / "manager/version.txt"
        manager.parent.mkdir()
        manager.write_text("new official manager revision\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "update manager without changing kernel ABI")
        self.assertNotEqual(self.head, self.run_git("rev-parse", "HEAD").stdout.strip())
        self.assertTrue(self.apply_fixture())
        # The new commit is still accepted on a complete idempotent second run.
        self.assertFalse(self.apply_fixture())

    def test_drift_in_either_reviewed_tree_refused_before_modification(self):
        for relative, tree in (("kernel/unpatched.c", "kernel"), ("uapi/ioctl.h", "uapi")):
            with self.subTest(tree=tree):
                (self.repository / relative).write_text("/* upstream source changed */\n", encoding="utf-8")
                self.run_git("add", ".")
                self.run_git("commit", "--quiet", "-m", f"change {tree} implementation")
                before = self.state()
                with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                        self.assertRaisesRegex(ValueError, f"requires the reviewed SukiSU {tree} tree"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("reset", "--hard", "--quiet", self.head)

    def add_reviewed_source_pair(self):
        (self.repository / 'kernel/new_feature.c').write_text('/* reviewed new native feature */\n', encoding='utf-8')
        self.uapi.write_text('/* reviewed new native UAPI */\n', encoding='utf-8')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'reviewed native source update')
        self.profile['reviewed_source_trees'] = [{
            'kernel': self.run_git('rev-parse', 'HEAD:kernel').stdout.strip(),
            'uapi': self.run_git('rev-parse', 'HEAD:uapi').stdout.strip()}]

    def test_two_reviewed_source_pairs_both_apply_and_reverify(self):
        self.add_reviewed_source_pair()
        newer = self.run_git('rev-parse', 'HEAD').stdout.strip()
        for reference in (self.head, newer):
            with self.subTest(reference=reference):
                self.run_git('reset', '--hard', '--quiet', reference)
                self.assertTrue(self.apply_fixture())
                self.assertFalse(self.apply_fixture())

    def test_crossed_reviewed_kernel_and_uapi_pairs_are_rejected(self):
        self.add_reviewed_source_pair()
        newer = self.run_git('rev-parse', 'HEAD').stdout.strip()
        for kernel_from, uapi_from in ((self.head, newer), (newer, self.head)):
            with self.subTest(kernel_from=kernel_from):
                self.run_git('reset', '--hard', '--quiet', newer)
                self.run_git('restore', '--source', kernel_from, '--staged', '--worktree', '--', 'kernel')
                self.run_git('restore', '--source', uapi_from, '--staged', '--worktree', '--', 'uapi')
                self.run_git('commit', '--quiet', '-m', 'mix separately reviewed sources')
                before = self.state()
                with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                        self.assertRaisesRegex(ValueError, 'complete kernel/UAPI pair'):
                    susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)
                self.assertEqual(before, self.state())

    def test_checked_patch_policy_accepts_unknown_sources_only_with_real_patch(self):
        self.add_reviewed_source_pair()
        del self.profile['reviewed_source_trees']
        self.profile.update(source_policy='checked-patch', version='v2.3.0')
        uapi = self.uapi.read_bytes()
        self.assertTrue(self.apply_fixture())
        self.assertFalse(self.apply_fixture())
        self.assertEqual(self.uapi.read_bytes(), uapi)
        self.assertEqual(self.run_git('diff', '--name-only', 'HEAD', '--', 'uapi').stdout, '')

    def test_checked_patch_policy_preserves_patch_and_native_validation_failures(self):
        self.add_reviewed_source_pair()
        del self.profile['reviewed_source_trees']
        self.profile.update(source_policy='checked-patch', version='v2.3.0')
        before = self.state()
        broken = self.patch.read_text().replace('-/* original kernel init */', '-/* incompatible source */')
        self.patch.write_text(broken, encoding='utf-8', newline='\n')
        with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                self.assertRaisesRegex(ValueError, 'Complete SUSFS 2.3 patch does not apply'):
            susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)
        self.assertEqual(before, self.state())
        self.providers[0].write_text('int ksu_patch_text(void) { return 0; }\n', encoding='utf-8')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'upstream provider regressed')
        with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                self.assertRaisesRegex(ValueError, 'Incomplete SukiSU text patch implementation'):
            susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)

    def test_checked_patch_policy_still_refuses_local_uapi_edits(self):
        self.profile.update(source_policy='checked-patch', version='v2.3.0')
        self.uapi.write_text('/* local ABI edit */\n', encoding='utf-8')
        with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                self.assertRaisesRegex(ValueError, 'UAPI files differ from the reviewed source'):
            susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)

    def test_port_patch_cannot_modify_uapi_even_when_normal_kernel_hunks_are_present(self):
        self.uapi.write_text('/* changed by patch */\n', encoding='utf-8')
        uapi_patch = self.run_git('diff', '--full-index').stdout
        self.run_git('restore', '.')
        self.patch.write_text(self.patch.read_text() + uapi_patch, encoding='utf-8', newline='\n')
        before = self.state()
        with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                self.assertRaisesRegex(ValueError, 'invalid file list'):
            susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)
        self.assertEqual(before, self.state())

    def test_reviewed_variant_uses_complete_patch_checks_and_reverifies(self):
        variant = self.patch.with_name('sukisu-susfs23-avc.patch')
        variant.write_text(self.patch.read_text(), encoding='utf-8', newline='\n')
        self.patch.write_text(self.patch.read_text().replace('-/* original kernel init */',
                                                           '-/* incompatible older init */'),
                              encoding='utf-8', newline='\n')
        with mock.patch.object(susfs, 'verify_integration') as verify:
            self.assertTrue(susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True))
            self.assertFalse(susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True))
            self.assertEqual(verify.call_count, 2)
        self.assertIn('integrated', self.core.read_text())

    def test_explicit_custom_patch_does_not_search_for_variants(self):
        variant = self.patch.with_name('sukisu-susfs23-avc.patch')
        variant.write_text(self.patch.read_text(), encoding='utf-8', newline='\n')
        self.patch.write_text(self.patch.read_text().replace('-/* original kernel init */',
                                                           '-/* incompatible older init */'),
                              encoding='utf-8', newline='\n')
        before = self.state()
        with mock.patch.object(susfs, 'load_profile', return_value=self.profile), \
                self.assertRaisesRegex(ValueError, 'Complete SUSFS 2.3 patch does not apply'):
            susfs.apply_integration(self.repository.parent, self.directory / 'susfs', self.patch)
        self.assertEqual(before, self.state())

    def three_way_fixture(self, *, conflicting=False):
        original = ('void kernel_init(void) {\n'
                    '    native_before();\n'
                    '    unchanged_gap();\n'
                    '    port_target();\n'
                    '    native_after();\n'
                    '}\n')
        self.core.write_text(original, encoding='utf-8', newline='\n')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'original port preimage')
        self.core.write_text(original.replace('port_target();', 'complete_port();'), encoding='utf-8', newline='\n')
        self.patch.write_text(self.run_git('diff', '--full-index').stdout, encoding='utf-8', newline='\n')
        self.run_git('restore', '.')
        updated = (original.replace('port_target();', 'upstream_replacement();') if conflicting
                   else original.replace('native_before();', 'native_before_updated();'))
        self.core.write_text(updated, encoding='utf-8', newline='\n')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'new official source context')
        self.assertNotEqual(susfs.git(self.repository, 'apply', '--check', str(self.patch)).returncode, 0)
        return updated

    def test_clean_three_way_merge_preserves_upstream_edit_and_reverifies(self):
        updated = self.three_way_fixture()
        with mock.patch.object(susfs, 'verify_integration') as verify:
            self.assertTrue(susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True))
            self.assertEqual(self.core.read_text(), updated.replace('port_target();', 'complete_port();'))
            self.assertFalse(susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True))
            self.assertEqual(verify.call_count, 4)

    def test_upstream_initialization_added_at_removed_blank_line_is_merged_and_reversed(self):
        original = ('void kernel_init(void) {\n    native_before();\n\n'
                    + ''.join(f'    unchanged_{index}();\n' for index in range(8))
                    + '    port_target();\n    native_after();\n}\n')
        self.core.write_text(original, encoding='utf-8', newline='\n')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'port base with separator')
        ported = original.replace('native_before();\n\n', 'native_before();\n').replace('port_target();', 'complete_port();')
        self.core.write_text(ported, encoding='utf-8', newline='\n')
        self.patch.write_text(self.run_git('diff', '--full-index').stdout, encoding='utf-8', newline='\n')
        self.run_git('restore', '.')
        updated = original.replace('\n\n', '\n\n    native_inode_init();\n\n')
        self.core.write_text(updated, encoding='utf-8', newline='\n')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'new upstream native initialization')
        with mock.patch.object(susfs, 'verify_integration'):
            self.assertTrue(susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True))
        actual = self.core.read_text(encoding='utf-8')
        self.assertIn('native_inode_init();', actual)
        self.assertIn('complete_port();', actual)
        self.assertNotIn('port_target();', actual)
        self.assertNotIn('<<<<<<<', actual)

    def test_three_way_check_success_cannot_authorize_a_real_conflict(self):
        updated = self.three_way_fixture(conflicting=True)
        check = susfs.git(self.repository, 'apply', '--3way', '--check', str(self.patch))
        self.assertEqual(check.returncode, 0)
        with mock.patch.object(susfs, 'verify_integration') as verify, \
                self.assertRaisesRegex(ValueError, 'Complete SUSFS 2.3 patch does not apply'):
            susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True)
        verify.assert_not_called()
        self.assertEqual(self.core.read_text(), updated)
        self.assertEqual(self.run_git('status', '--porcelain').stdout, '')

    def test_clean_three_way_merge_missing_native_interface_never_changes_source(self):
        updated = self.three_way_fixture()
        with mock.patch.object(susfs, 'verify_integration', side_effect=ValueError('required interface missing')), \
                self.assertRaisesRegex(ValueError, 'Complete SUSFS 2.3 patch does not apply'):
            susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True)
        self.assertEqual(self.core.read_text(), updated)
        self.assertEqual(self.run_git('status', '--porcelain').stdout, '')

    def test_three_way_materialization_never_overwrites_local_edits(self):
        self.three_way_fixture()
        edited = self.core.read_text().replace('native_after();', 'local_custom_change();')
        self.core.write_text(edited, encoding='utf-8', newline='\n')
        with mock.patch.object(susfs, 'verify_integration'), \
                self.assertRaisesRegex(ValueError, 'differ from the pinned source'):
            susfs.apply_reviewed_port(self.repository, self.patch, allow_variants=True)
        self.assertEqual(self.core.read_text(), edited)

    def test_native_rcu_allowlist_lookup_and_decisions_survive_the_port(self):
        policy = self.repository / 'kernel/policy'
        policy.mkdir()
        allowlist = policy / 'allowlist.c'
        native = '''bool ksu_uid_should_umount(uid_t uid) {
    if (likely(ksu_is_manager_appid_valid()) && unlikely(ksu_get_manager_appid() == uid % PER_USER_RANGE)) {
        // Original manager exemption removed by the SUSFS direct-hook port.
        return false;
    }
    struct perm_data *p;
    bool res;
    rcu_read_lock();
    res = default_non_root_profile.umount_modules;
    hash_for_each_possible_rcu (allow_list, p, list, uid) {
        if (p->profile.curr_uid != uid) continue;
        if (p->profile.allow_su) res = false;
        else if (!p->profile.nrp_config.use_default) res = p->profile.nrp_config.profile.umount_modules;
        break;
    }
    rcu_read_unlock();
    return res;
}
'''
        allowlist.write_text(native, encoding='utf-8')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'new native RCU lookup')
        ported = native[native.index('    struct perm_data'):]
        ported = 'bool ksu_uid_should_umount(uid_t uid) {\n' + ported
        allowlist.write_text(ported, encoding='utf-8')
        susfs.verify_native_rcu_allowlist(self.repository)
        for broken in (ported.replace('return res;', 'return !res;'),
                       ported.replace('hash_for_each_possible_rcu', 'old_profile_lookup')):
            allowlist.write_text(broken, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'native RCU allowlist'):
                susfs.verify_native_rcu_allowlist(self.repository)

    def test_local_uapi_drift_and_staged_drift_are_rejected(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                self.uapi.write_text("/* local ABI change */\n", encoding="utf-8")
                if staged:
                    self.run_git("add", ".")
                before = self.state()
                with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                        self.assertRaisesRegex(ValueError, "UAPI files differ from the reviewed source"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("reset", "--hard", "--quiet", self.head)

    def test_reviewed_kernel_tree_alone_cannot_skip_uapi_verification(self):
        profile = {key: value for key, value in self.profile.items() if key != "reviewed_uapi_tree"}
        before = self.state()
        with mock.patch.object(susfs, "load_profile", return_value=profile), \
                self.assertRaisesRegex(ValueError, "missing a valid reviewed_uapi_tree"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_legacy_commit_profile_remains_strict_without_tree_review(self):
        profile = {"ksu_commits": [self.head]}
        with mock.patch.object(susfs, "load_profile", return_value=profile):
            susfs.check_versions(self.repository, self.directory / "susfs")
        self.run_git("commit", "--quiet", "--allow-empty", "-m", "not explicitly reviewed")
        with mock.patch.object(susfs, "load_profile", return_value=profile), \
                self.assertRaisesRegex(ValueError, "requires SukiSU reviewed"):
            susfs.check_versions(self.repository, self.directory / "susfs")

    def test_wrong_susfs_version_refused_before_modification(self):
        self.header.write_text('#define SUSFS_VERSION "v2.2.0"\n', encoding="utf-8")
        before = self.state()
        with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                self.assertRaisesRegex(ValueError, "requires v2.3.0"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_source_drift_and_staged_drift_refused_without_overwriting(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                self.core.write_text("/* local change */\n", encoding="utf-8", newline="\n")
                if staged:
                    self.run_git("add", ".")
                before = self.state()
                with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                        self.assertRaisesRegex(ValueError, "differ from the pinned source"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("reset", "--hard", "--quiet", "HEAD")

    def test_one_failed_hunk_does_not_apply_other_hunks(self):
        content = self.patch.read_text().replace("-/* original kernel init */", "-/* another upstream revision */")
        self.patch.write_text(content, encoding="utf-8", newline="\n")
        before = self.state()
        with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                self.assertRaisesRegex(ValueError, "Complete SUSFS 2.3 patch does not apply"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_partial_prior_application_is_rejected(self):
        self.core.write_text("/* integrated kernel init */\n", encoding="utf-8", newline="\n")
        before = self.state()
        with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                self.assertRaisesRegex(ValueError, "partial or conflicting patch"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_complete_reverse_check_does_not_skip_integration_validation(self):
        self.apply_fixture()
        before = self.state()
        with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                mock.patch.object(susfs, "verify_integration", side_effect=ValueError("missing runner")), \
                self.assertRaisesRegex(ValueError, "missing runner"):
            susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
        self.assertEqual(before, self.state())

    def test_missing_or_modified_text_provider_fails_before_applying_any_hunk(self):
        for removed in (False, True):
            with self.subTest(removed=removed):
                provider = self.providers[0]
                if removed:
                    provider.unlink()
                else:
                    provider.write_text("int ksu_patch_text(void) { return 0; }\n",
                                        encoding="utf-8", newline="\n")
                before = self.state()
                with mock.patch.object(susfs, "load_profile", return_value=self.profile), \
                        self.assertRaisesRegex(ValueError, "text patch providers differ"):
                    susfs.apply_integration(self.repository.parent, self.directory / "susfs", self.patch)
                self.assertEqual(before, self.state())
                self.run_git("restore", ".")

    def test_native_avc_provider_cannot_be_replaced_or_unlinked(self):
        feature = self.repository / 'kernel/feature'
        feature.mkdir()
        provider = feature / 'avc_spoof.c'
        provider.write_text('void ksu_avc_spoof_init(void) { real_init(); }\n', encoding='utf-8')
        (feature / 'avc_spoof.h').write_text('void ksu_avc_spoof_init(void);\n', encoding='utf-8')
        runtime = self.repository / 'kernel/runtime'
        runtime.mkdir()
        (runtime / 'boot_event.c').write_text(AvcLifecycleTests.BOOT, encoding='utf-8')
        self.core.write_text(AvcLifecycleTests.CORE, encoding='utf-8')
        self.build.write_text('kernelsu-objs += feature/avc_spoof.o\n', encoding='utf-8')
        self.run_git('add', '.')
        self.run_git('commit', '--quiet', '-m', 'native AVC feature')
        susfs.verify_native_avc(self.repository)
        provider.write_text('void ksu_avc_spoof_init(void) {}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'selected, real implementation'):
            susfs.verify_native_avc(self.repository)
        self.run_git('restore', '.')
        self.build.write_text('# kernelsu-objs += feature/avc_spoof.o\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'linked in Kbuild'):
            susfs.verify_native_avc(self.repository)


class SelinuxInodeLifecycleTests(unittest.TestCase):
    CORE = '''int kernelsu_init(void) {
    int ret;
    ksu_init_symbol_resolver();
    ret = ksu_selinux_inode_init();
    if (ret) {
        put_cred(ksu_cred);
        ksu_cred = NULL;
        return ret;
    }
    susfs_init();
    return 0;
}
'''

    def setUp(self):
        import tempfile
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT.parent / '.gki-upstream-cache')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.core = self.root / 'kernel/core/init.c'
        self.core.parent.mkdir(parents=True)
        self.core.write_text(self.CORE, encoding='utf-8')
        provider = self.root / 'kernel/selinux'
        provider.mkdir()
        (provider / 'inode.c').write_text('int ksu_selinux_inode_init(void) { return real_init(); }', encoding='utf-8')
        (provider / 'selinux.h').write_text('int ksu_selinux_inode_init(void);', encoding='utf-8')
        (self.root / 'kernel/Kbuild').write_text('kernelsu-objs += selinux/inode.o\n', encoding='utf-8')
        result = subprocess.CompletedProcess([], 0, stdout=self.CORE, stderr='')
        mocking = mock.patch.object(susfs, 'git', return_value=result)
        mocking.start()
        self.addCleanup(mocking.stop)

    def test_selected_initialization_and_credential_rollback_are_retained(self):
        susfs.verify_native_selinux_inode(self.root)

    def test_removed_call_rollback_or_return_variable_is_rejected(self):
        for before, after in (('ret = ksu_selinux_inode_init();', ''), ('put_cred(ksu_cred);', ''),
                              ('ksu_cred = NULL;', ''), ('return ret;', 'return 0;'), ('int ret;', '')):
            with self.subTest(before=before):
                self.core.write_text(self.CORE.replace(before, after), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'credential error cleanup'):
                    susfs.verify_native_selinux_inode(self.root)


class AvcLifecycleTests(unittest.TestCase):
    CORE = '''#include "feature/avc_spoof.h"
void init(void) { ksu_avc_spoof_init(); }
void exit(void) { ksu_avc_spoof_exit(); }
'''
    BOOT = '''#include "feature/avc_spoof.h"
void post_fs_data(void) { ksu_avc_spoof_late_init(); }
'''

    def test_native_init_teardown_and_late_init_are_preserved(self):
        susfs.verify_avc_lifecycle(self.CORE, self.BOOT)

    def test_missing_init_exit_or_late_init_cannot_hide_behind_comments(self):
        for symbol, core, boot in (
                ('ksu_avc_spoof_init', self.CORE.replace('ksu_avc_spoof_init();', '/* ksu_avc_spoof_init(); */'), self.BOOT),
                ('ksu_avc_spoof_exit', self.CORE.replace('ksu_avc_spoof_exit();', ''), self.BOOT),
                ('ksu_avc_spoof_late_init', self.CORE, self.BOOT.replace('ksu_avc_spoof_late_init();', ''))):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, symbol):
                susfs.verify_avc_lifecycle(core, boot)

    def test_duplicate_calls_and_missing_native_include_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'exactly one ksu_avc_spoof_init'):
            susfs.verify_avc_lifecycle(self.CORE + 'void extra(void) { ksu_avc_spoof_init(); }', self.BOOT)
        with self.assertRaisesRegex(ValueError, 'native feature header'):
            susfs.verify_avc_lifecycle(self.CORE.replace('#include "feature/avc_spoof.h"', ''), self.BOOT)


class PatchMemoryBuildTests(unittest.TestCase):
    RULES = """ifeq ($(CONFIG_ARM64),y)
kernelsu-objs += hook/arm64/patch_memory.o
else ifeq ($(CONFIG_X86_64),y)
kernelsu-objs += hook/x86_64/patch_memory.o
else ifeq ($(CONFIG_RISCV),y)
kernelsu-objs += hook/riscv64/patch_memory.o
endif
"""

    def test_arch_providers_remain_available_without_dynamic_syscall_registration(self):
        susfs.verify_patch_memory_build(self.RULES)

    def test_dropped_arch_provider_cannot_pass_integration_validation(self):
        for arch in susfs.PATCH_MEMORY_ARCHES:
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, "real patch_memory object"):
                susfs.verify_patch_memory_build(self.RULES.replace(f"hook/{arch}/patch_memory.o", ""))

    def test_old_syscall_registration_cannot_be_restored_as_a_provider_fix(self):
        with self.assertRaisesRegex(ValueError, "Legacy syscall hook registration"):
            susfs.verify_patch_memory_build(self.RULES + "kernelsu-objs += hook/syscall_hook_manager.o\n")


class ModuleBlacklistLifecycleTests(unittest.TestCase):
    CORE = "void kernelsu_exit(void) { ksu_module_blacklist_exit(); }\n"
    RUNTIME = '''void second_stage(void) {
    if (check_argv(*argv, 1, "second_stage", buf, sizeof(buf))) {
        setup_ksu_cred();
        ksu_feature_apply_forced();
        ksu_module_blacklist_apply();
        static_branch_disable(&is_init_second_stage_not_executed);
    }
}
'''

    def test_sukisu_static_param_and_second_stage_lifecycle_is_preserved(self):
        susfs.verify_blacklist_lifecycle(self.CORE, self.RUNTIME)

    def test_foreign_kernelsu_init_api_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "foreign init hook"):
            susfs.verify_blacklist_lifecycle(self.CORE + "void init(void) { ksu_module_blacklist_init(); }",
                                             self.RUNTIME)

    def test_moving_apply_to_early_core_init_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "before second-stage"):
            susfs.verify_blacklist_lifecycle(self.CORE + "void init(void) { ksu_module_blacklist_apply(); }",
                                             self.RUNTIME.replace("ksu_module_blacklist_apply();", ""))

    def test_missing_second_stage_apply_or_exit_cannot_silently_disable_feature(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            susfs.verify_blacklist_lifecycle(self.CORE, self.RUNTIME.replace("ksu_module_blacklist_apply();", ""))
        with self.assertRaisesRegex(ValueError, "teardown hook"):
            susfs.verify_blacklist_lifecycle("", self.RUNTIME)

    def test_apply_outside_second_stage_guard_is_rejected(self):
        runtime = (self.RUNTIME.replace("ksu_module_blacklist_apply();", "")
                   + "void other(void) { ksu_module_blacklist_apply(); }")
        with self.assertRaisesRegex(ValueError, "exactly once"):
            susfs.verify_blacklist_lifecycle(self.CORE, runtime)


class PreinitDescriptorCleanupTests(unittest.TestCase):
    PREINIT = '''#include "util.h"
int setup(void) {
    if (kern_path(fd_path, LOOKUP_FOLLOW, &path)) {
        ksu_close_fd(fd);
        return -EINVAL;
    }
    return fd;
}
int post_exec(void) {
    if (*retval && *preinit_fd > 0) {
        ksu_close_fd(*preinit_fd);
        return -EINVAL;
    }
    return 0;
}
'''
    UTIL = '#define ksu_close_fd(fd) ({ ksyscall(close, fd); })\n'

    def test_native_close_wrapper_cleans_both_preinit_error_paths(self):
        susfs.verify_preinit_fd_cleanup(self.PREINIT, self.UTIL)

    def test_direct_close_fd_regression_is_rejected_even_when_post_exec_uses_wrapper(self):
        bad = self.PREINIT.replace('ksu_close_fd(fd);', 'close_fd(fd);', 1)
        with self.assertRaisesRegex(ValueError, 'unavailable on GKI 5.10'):
            susfs.verify_preinit_fd_cleanup(bad, self.UTIL)

    def test_removing_either_cleanup_path_is_rejected(self):
        for call in ('ksu_close_fd(fd);', 'ksu_close_fd(*preinit_fd);'):
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, 'both real descriptor cleanup paths'):
                susfs.verify_preinit_fd_cleanup(self.PREINIT.replace(call, ''), self.UTIL)

    def test_stub_and_commented_out_syscall_helpers_are_rejected(self):
        for util in ('#define ksu_close_fd(fd) ((void)0)\n', '/*\n' + self.UTIL + '*/\n'):
            with self.subTest(util=util), self.assertRaisesRegex(ValueError, 'native close syscall implementation'):
                susfs.verify_preinit_fd_cleanup(self.PREINIT, util)

    def test_missing_native_helper_include_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'include its native descriptor cleanup helper'):
            susfs.verify_preinit_fd_cleanup(self.PREINIT.replace('#include "util.h"', ''), self.UTIL)


if __name__ == "__main__":
    unittest.main()
