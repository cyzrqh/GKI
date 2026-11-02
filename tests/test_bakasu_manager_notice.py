"""Guard the narrow BakaSU-only warning patch and its signing trust boundary."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import configure_bakasu_manager_notice as notice

# The relevant unmodified fragments are from official BakaSU commit
# d5e9e025b8b49155f8014684d64c91742b465f65. Wrappers omit unrelated UI/code.
SOURCES = {
    'cli': 'package org.bakasu.bakasu.data.shell\n\nclass KsuCliRepository(context: Context) {\n'
           + notice.OFFICIAL_METHOD + '\n    fun untouchedDaemonOperation() = "unchanged"\n}\n',
    'model': '''package org.bakasu.bakasu.domain.model
data class KernelStatus(
    val isManager: Boolean = false,
    val isFullFeatured: Boolean = false,
    val isOfficialSignature: Boolean = true,
    val isSafeMode: Boolean = false,
)
''',
    'repository': 'package org.bakasu.bakasu.data.kernel\n'
                  'class KernelRepository {\n    suspend fun getStatus() {\n'
                  '        val isManager = runCatching { Natives.isManager }.getOrDefault(false)\n'
                  '        KernelStatus(\n            isManager = isManager,\n'
                  + notice.OFFICIAL_STATUS + '            isSafeMode = Natives.isSafeMode,\n        )\n    }\n}\n',
    'home': '''package org.bakasu.bakasu.ui.screen.main
fun HomePage() {
                if (!uiState.systemStatus.isOfficialSignature) {
                    WarningCard(message = stringResource(R.string.unofficial_version_notice))
                }
                if (BuildConfig.IS_PR_BUILD || uiState.systemStatus.isPrBuild) {
                    WarningCard(message = stringResource(R.string.home_pr_build_warning))
                }
}
''',
    'navigation': '''enum class BottomBarDestination(val rootRequired: Boolean) {
    Kpm(
        Icons.TwoTone.Memory,
        Icons.TwoTone.Memory,
        false,
    ),
    SuperUser(true),
}
''',
}
IDENTITY = {'manager_package': 'com.resukisu.resukisu', 'manager_cert_size': 730,
            'manager_cert_sha256': 'c8e9af2b51e0f76b4e9133726c17a8034a131fec4235f6b779ab8b1e83127c12'}


class BakaSUManagerNoticeTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.bakasu-notice-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        kbuild = self.root / 'kernel/Kbuild'
        kbuild.parent.mkdir()
        kbuild.write_text('REPO_NAME := BakaSU\n', encoding='utf-8')
        self.paths = {}
        for key, relative in notice.FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SOURCES[key], encoding='utf-8', newline='\n')
            self.paths[key] = path
        (self.root / 'README.md').write_bytes(b'User requested no README edits\r\n')
        self.identity_file = self.root / 'public.json'
        self.identity_file.write_text(json.dumps(IDENTITY), encoding='utf-8')

    def state(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def apply(self, identity=None):
        return notice.configure_notice(self.root, IDENTITY if identity is None else identity)

    def test_original_official_author_check_and_other_authorization_code_are_preserved(self):
        before = self.state()
        changed = self.apply()
        self.assertEqual(set(notice.FILES.values()), set(changed))
        cli = self.paths['cli'].read_text(encoding='utf-8')
        self.assertEqual(1, cli.count(notice.OFFICIAL_METHOD))
        self.assertIn('size: 0x377, hash: d3469712b6214462764a1d8d3e5cbe1d6819a0b629791b9f4101867821f1df64', cli)
        self.assertIn('fun untouchedDaemonOperation() = "unchanged"', cli)
        self.assertNotIn('return true', cli)
        self.assertIn('isFullFeatured: Boolean = false', self.paths['model'].read_text(encoding='utf-8'))
        self.assertIn('isSafeMode = Natives.isSafeMode,', self.paths['repository'].read_text(encoding='utf-8'))
        home = self.paths['home'].read_text(encoding='utf-8')
        self.assertIn('R.string.unofficial_version_notice', home)
        self.assertIn('if (BuildConfig.IS_PR_BUILD || uiState.systemStatus.isPrBuild)', home)
        self.assertIn('R.string.home_pr_build_warning', home)
        after = self.state()
        self.assertEqual(set(before), set(after))
        for relative in set(before) - set(notice.FILES.values()):
            self.assertEqual(before[relative], after[relative])

    def test_installed_signer_is_checked_locally_even_without_root(self):
        self.apply()
        cli = self.paths['cli'].read_text(encoding='utf-8')
        method = cli.split('// ' + notice.MARKER, 1)[1].split('fun untouchedDaemonOperation', 1)[0]
        self.assertIn('packageName != "com.resukisu.resukisu" || packageName != installed.packageName', method)
        self.assertIn('packageResourcePath != installed.packageResourcePath', method)
        self.assertIn('GET_SIGNING_CERTIFICATES', method)
        self.assertIn('GET_SIGNATURES', method)
        self.assertIn('.signingInfo?.apkContentsSigners', method)
        self.assertIn('signatures == null || signatures.size != 1', method)
        self.assertIn('certificate.size != 730', method)
        self.assertIn('MessageDigest.getInstance("SHA-256")', method)
        self.assertIn('digest == "' + IDENTITY['manager_cert_sha256'] + '"', method)
        self.assertNotIn('getRootShell', method)
        self.assertNotIn('isManager', method)
        self.assertNotIn('signingCertificateHistory', method)
        repository = self.paths['repository'].read_text(encoding='utf-8')
        self.assertIn('isTrustedRepositoryBuild = runCatching {', repository)
        self.assertIn('isManager = isManager,', repository)
        self.assertIn('application.packageName,\n                    application.packageResourcePath,', repository)
        self.assertIn('}.getOrDefault(false),', repository)
        model = self.paths['model'].read_text(encoding='utf-8')
        self.assertIn('val isTrustedRepositoryBuild: Boolean = false,', model)
        home = self.paths['home'].read_text(encoding='utf-8')
        self.assertIn('!uiState.systemStatus.isOfficialSignature &&\n'
                      '                    !uiState.systemStatus.isTrustedRepositoryBuild', home)

    def test_identity_is_not_hardcoded_to_this_machine_or_a_different_signer(self):
        other = {**IDENTITY, 'manager_cert_size': 1024, 'manager_cert_sha256': 'b' * 64}
        self.apply(other)
        cli = self.paths['cli'].read_text(encoding='utf-8')
        self.assertIn('certificate.size != 1024', cli)
        self.assertIn('digest == "' + 'b' * 64 + '"', cli)
        self.assertNotIn(IDENTITY['manager_cert_sha256'], cli)

    def test_same_identity_is_idempotent_and_does_not_rewrite_files(self):
        self.apply()
        before = self.state()
        with mock.patch.object(notice.os, 'replace', side_effect=AssertionError('must not rewrite')):
            self.assertEqual([], self.apply())
        self.assertEqual(before, self.state())

    def test_changed_signer_cannot_reuse_a_patch_from_another_identity(self):
        self.apply()
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'signer changed'):
            self.apply({**IDENTITY, 'manager_cert_sha256': 'b' * 64})
        self.assertEqual(before, self.state())

    def test_drift_in_the_last_source_does_not_partially_patch_earlier_sources(self):
        path = self.paths['home']
        path.write_text(path.read_text(encoding='utf-8').replace('if (!uiState.systemStatus.isOfficialSignature)', 'if (changedCondition)'), encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'source layout changed'):
            self.apply()
        self.assertEqual(before, self.state())

    def test_modified_official_method_is_rejected_without_any_changes(self):
        path = self.paths['cli']
        path.write_text(path.read_text(encoding='utf-8').replace('out.firstOrNull()?.trim()', 'out.single().trim()'), encoding='utf-8')
        before = self.state()
        with self.assertRaises(notice.NoticeError):
            self.apply()
        self.assertEqual(before, self.state())

    def test_partial_or_duplicate_patch_is_rejected(self):
        self.apply()
        self.paths['home'].write_text(SOURCES['home'], encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'partial or duplicate'):
            self.apply()
        self.assertEqual(before, self.state())
        self.paths['home'].write_text('// ' + notice.MARKER + '\n// ' + notice.MARKER + '\n' + SOURCES['home'], encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'partial or duplicate'):
            self.apply()
        self.assertEqual(before, self.state())

    def test_sukisu_variant_is_rejected_without_editing_any_files(self):
        (self.root / 'kernel/Kbuild').write_text('REPO_NAME := SukiSU\n', encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'limited to native BakaSU'):
            self.apply()
        self.assertEqual(before, self.state())

    def test_both_upstream_native_names_are_supported(self):
        (self.root / 'kernel/Kbuild').write_text('REPO_NAME := ReSukiSU\n', encoding='utf-8')
        self.assertEqual(5, len(self.apply()))

    def test_kpm_navigation_uses_the_native_root_required_gate(self):
        self.apply()
        navigation = self.paths['navigation'].read_text(encoding='utf-8')
        self.assertIn('Icons.TwoTone.Memory,\n        true,', navigation)
        self.assertIn('SuperUser(true)', navigation)

    def test_crlf_is_preserved(self):
        for key, path in self.paths.items():
            path.write_bytes(SOURCES[key].replace('\n', '\r\n').encode('utf-8'))
        self.apply()
        for path in self.paths.values():
            raw = path.read_bytes()
            self.assertEqual(raw.count(b'\n'), raw.count(b'\r\n'))
        self.assertEqual([], self.apply())

    def test_unexpected_extra_trust_field_is_rejected(self):
        self.paths['repository'].write_text(SOURCES['repository'] + '\nval isTrustedRepositoryBuild = true\n', encoding='utf-8')
        before = self.state()
        with self.assertRaisesRegex(notice.NoticeError, 'outside the expected patch'):
            self.apply()
        self.assertEqual(before, self.state())

    def test_write_failure_rolls_back_earlier_replacements_and_removes_staging(self):
        before = self.state()
        original_replace = notice.os.replace
        calls = []
        def fail_second(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise OSError('simulated replacement failure')
            return original_replace(source, target)
        with mock.patch.object(notice.os, 'replace', side_effect=fail_second):
            with self.assertRaisesRegex(OSError, 'simulated replacement'):
                self.apply()
        self.assertEqual(before, self.state())

    def test_invalid_public_identities_are_rejected_before_any_source_changes(self):
        values = [None, [], {}, {**IDENTITY, 'schema_version': True}, {**IDENTITY, 'schema_version': 2},
                  {**IDENTITY, 'manager_package': 'com.sukisu.ultra'},
                  {**IDENTITY, 'manager_cert_size': True}, {**IDENTITY, 'manager_cert_size': '730'},
                  {**IDENTITY, 'manager_cert_size': 63}, {**IDENTITY, 'manager_cert_size': 4097},
                  {**IDENTITY, 'manager_cert_sha256': 'X' * 64},
                  {**IDENTITY, 'manager_cert_sha256': 'a' * 63},
                  {**IDENTITY, 'manager_cert_sha256': 'a' * 63 + '\n'},
                  {**IDENTITY, 'keystore': 'must never be consumed'}]
        for identity in values:
            with self.subTest(identity=identity):
                before = self.state()
                with self.assertRaises(notice.NoticeError):
                    notice.configure_notice(self.root, identity)
                self.assertEqual(before, self.state())

    def test_schema_version_one_and_duplicate_json_handling(self):
        self.assertEqual(IDENTITY, notice.validate_identity({**IDENTITY, 'schema_version': 1}))
        self.identity_file.write_text('{"manager_package":"first","manager_package":"second"}', encoding='utf-8')
        with self.assertRaises(notice.NoticeError):
            notice.read_identity(self.identity_file)

    def test_cli_accepts_only_public_identity_and_reports_errors(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, notice.main(['--source', str(self.root), '--identity', str(self.identity_file)]))
        self.assertIn('original official-author and kernel signature checks retained', output.getvalue())
        self.identity_file.write_text('{"manager_cert_sha256":null}', encoding='utf-8')
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertEqual(1, notice.main(['--source', str(self.root), '--identity', str(self.identity_file)]))
        self.assertIn('configuration failed', error.getvalue())


if __name__ == '__main__':
    unittest.main()
