"""Extract the production patch's backend and exercise its real C on Linux.

Only the host kernel/memory/KernelPatch interfaces are simulated by fixtures.
The implementation under test always comes from tools/patches/bakasu-kpm.patch.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / 'tools/patches/bakasu-kpm.patch'
FIXTURE = ROOT / 'tests/fixtures/bakasu-kpm-backend'
BACKEND_FILES = frozenset(('kernel/kpm/kpm.c', 'kernel/kpm/kpm.h',
                           'kernel/kpm/bridge.h', 'uapi/kpm.h'))


def selected_patch() -> Path:
    # Explicit development override while the final combined patch is authored;
    # CI and normal local runs read the committed production patch by default.
    return Path(os.environ.get('GKI_BAKASU_KPM_PATCH', str(PATCH))).absolute()


def patch_sections(patch: Path) -> dict[str, str]:
    if (patch.is_symlink() or not patch.is_file()
            or not 1 <= patch.stat().st_size <= 4 * 1024 * 1024):
        raise ValueError('Expected the bounded, regular production KPM source patch')
    with patch.open('rb') as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if not 1 <= len(raw) <= 4 * 1024 * 1024:
        raise ValueError('Production KPM patch changed beyond its bounded size')
    text = raw.decode('utf-8')
    sections = {}
    for entry in re.split(r'(?m)^diff --git ', text)[1:]:
        first, _, body = entry.partition('\n')
        match = re.fullmatch(r'a/(\S+) b/(\S+)', first)
        if match is None or match[1] != match[2] or match[1] in sections:
            raise ValueError('Production KPM patch has an ambiguous source path')
        sections[match[1]] = body
    return sections


def added_lines(section: str) -> str:
    return '\n'.join(line[1:] for line in section.splitlines()
                     if line.startswith('+') and not line.startswith('+++'))


def remove_work(root: Path):
    root = root.resolve()
    if root.parent != ROOT / 'tests' or not root.name.startswith('.bakasu-kpm-backend-'):
        raise ValueError('Refusing backend test cleanup outside its owned temporary directory')
    shutil.rmtree(root)


@contextmanager
def extract_backend(patch: Path | None = None):
    """Use Git's production patch parser, without copying an implementation fixture."""
    patch = selected_patch() if patch is None else Path(patch).absolute()
    sections = patch_sections(patch)
    for relative in BACKEND_FILES:
        if (relative not in sections or 'new file mode 100644\n' not in sections[relative]
                or '--- /dev/null\n' not in sections[relative]):
            raise ValueError('Production patch must introduce the four regular KPM backend sources')
    work = ROOT / 'tests' / ('.bakasu-kpm-backend-' + uuid.uuid4().hex)
    work.mkdir()
    try:
        def git(*arguments):
            result = subprocess.run(['git', '-c', 'init.templateDir=', '-c', 'core.autocrlf=false',
                                     '-C', str(work), *arguments], capture_output=True,
                                    text=True, encoding='utf-8', timeout=30)
            if result.returncode:
                raise AssertionError('Production backend patch did not apply with Git: ' + result.stderr[-1500:])
        git('init', '-q')
        includes = ['--include=kernel/kpm/*', '--include=uapi/kpm.h']
        git('apply', '--check', *includes, str(patch))
        git('apply', *includes, str(patch))
        actual = {path.relative_to(work).as_posix() for directory in ('kernel', 'uapi')
                  for path in (work / directory).rglob('*') if path.is_file() or path.is_symlink()}
        if actual != BACKEND_FILES:
            raise AssertionError('Production patch extracted an incomplete or unexpected backend source set')
        for relative in BACKEND_FILES:
            path = work / relative
            if (path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 1048576
                    or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(work))):
                raise AssertionError('Backend source must be a bounded regular file within this extraction')
        yield work
    finally:
        remove_work(work)


@unittest.skipUnless(shutil.which('git'), 'Git is required to extract the production source patch')
class BakaKpmBackendTests(unittest.TestCase):
    def test_production_patch_extracts_real_backend_with_fixed_public_api(self):
        with extract_backend() as source:
            header = (source / 'uapi/kpm.h').read_text(encoding='utf-8')
            constants = dict(re.findall(r'static const __u32 (SUKISU_KPM_\w+)\s*=\s*([0-9]+);', header))
            self.assertEqual({'SUKISU_KPM_' + name: str(index) for index, name in enumerate(
                ('LOAD', 'UNLOAD', 'NUM', 'LIST', 'INFO', 'CONTROL', 'VERSION', 'LOAD_EVENT'), 1)}, constants)
            command = re.search(r'struct ksu_kpm_cmd\s*\{([^}]+)\};', header, re.S)[1]
            self.assertEqual(['control_code', 'arg1', 'arg2', 'result_code'],
                             re.findall(r'__aligned_u64\s+(\w+);', command))
            self.assertNotIn('packed', command)
            self.assertIn('signed 32-bit', header)
            bridge = (source / 'kernel/kpm/bridge.h').read_text(encoding='utf-8')
            self.assertRegex(bridge, r'#define SUKISU_KPM_ABI_VERSION\s+2\b')

    def test_dedicated_ioctl_retains_manager_root_gate_and_does_not_repurpose_existing_ids(self):
        sections = patch_sections(selected_patch())
        dispatch = added_lines(sections['kernel/supercall/dispatch.c'])
        handler = re.search(r'\.cmd\s*=\s*KSU_IOCTL_KPM,(.*?)\n\s*\},', dispatch, re.S)[1]
        self.assertIn('.handler = do_kpm', handler)
        self.assertIn('.perm_check = manager_or_root', handler)
        self.assertNotIn('always_allow', handler)
        self.assertNotIn('allow_su_session', handler)
        header = added_lines(sections['uapi/supercall.h'])
        self.assertEqual(1, header.count("_IOWR('K', 201, struct ksu_kpm_cmd)"))
        features = added_lines(sections['uapi/feature.h'])
        self.assertRegex(features, r'KSU_FEATURE_KPM\s*=\s*7\b')
        # The patch may show old fields as context, but cannot change them.
        for path in ('uapi/feature.h', 'uapi/supercall.h'):
            changes = '\n'.join(line[1:] for line in sections[path].splitlines()
                                if line.startswith(('+', '-')) and not line.startswith(('+++', '---')))
            self.assertNotRegex(changes, r'KERNEL_SU_UAPI_VERSION\s*=|KSU_FEATURE_ADB_ROOT\s*=')
            self.assertNotRegex(changes, r'KSU_IOCTL_\w+[^\n]*[\'\"]K[\'\"],\s*(?:102|200)\b')
        self.assertNotIn('kernel/supercall/perm.c', sections)

    def test_capability_is_read_only_and_uses_a_real_bridge_probe(self):
        with extract_backend() as source:
            code = (source / 'kernel/kpm/kpm.c').read_text(encoding='utf-8')
            self.assertIn('.feature_id = KSU_FEATURE_KPM', code)
            self.assertIn('.set_handler = NULL', code)
            self.assertRegex(code, r'\*value\s*=\s*sukisu_kpm_bridge\(SUKISU_KPM_PROBE,\s*0,\s*0,\s*0,\s*0\)\s*==\s*SUKISU_KPM_ABI_VERSION')
            self.assertNotIn('compact.h', code)
            self.assertNotIn('super_access.h', code)
            self.assertRegex(code, r'copy_to_user\(u64_to_user_ptr\(cmd\.result_code\),\s*&result,\s*sizeof\(result\)\)')

    @unittest.skipUnless(sys.platform == 'linux' and struct.calcsize('l') == 8
                         and (shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')),
                         'Real backend C execution requires an LP64 Linux host with GCC or Clang')
    def test_real_c_standard_operations_errors_abi2_and_disabled_backend(self):
        compiler = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
        with extract_backend() as source:
            include = source / 'host-include'
            for relative in ('linux/types.h', 'linux/init.h', 'linux/errno.h', 'linux/slab.h',
                             'linux/string.h', 'linux/uaccess.h', 'linux/kernel.h', 'asm/ptrace.h',
                             'infra/symbol_resolver.h', 'policy/feature.h'):
                path = include / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#include "harness.h"\n', encoding='utf-8')
            common = [compiler, '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                      '-Wno-unused-function', '-Wno-unused-parameter', '-Wno-unused-variable',
                      # The native Linux kernel uses this warning policy too;
                      # user-copy length checks compare a checked signed rc.
                      '-Wno-sign-compare', '-I' + str(include), '-I' + str(FIXTURE), '-I' + str(source)]
            for name, fixture, flags in (('enabled', 'harness.c', ['-DCONFIG_KPM=1', '-DCONFIG_ARM64=1']),
                                         ('disabled', 'disabled.c', [])):
                output = source / ('host-backend-' + name)
                built = subprocess.run([*common, *flags, str(FIXTURE / fixture), '-o', str(output)],
                                       capture_output=True, text=True, timeout=60)
                self.assertEqual(0, built.returncode, built.stdout + built.stderr)
                ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=15)
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn('real', ran.stdout)


if __name__ == '__main__':
    unittest.main()
