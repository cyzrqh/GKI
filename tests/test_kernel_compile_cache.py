"""Pinned tool installation and real compiler/configuration guard contracts."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import kernel_compile_cache as cache


def elf_fixture():
    value = bytearray(256)
    value[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', value, 16, 3, 62, 1)
    struct.pack_into('<Q', value, 32, 64)
    struct.pack_into('<HH', value, 54, 56, 1)
    return bytes(value)


def posix(path):
    value = Path(path).resolve().as_posix()
    return '/' + value[0].lower() + value[2:] if os.name == 'nt' else value


def bash():
    if shell := shutil.which('bash'):
        return shell
    if git := shutil.which('git'):
        candidate = Path(git).parent.parent / 'usr/bin/sh.exe'
        if candidate.is_file():
            return str(candidate)
    return None


class KernelCacheGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / 'tests' / ('.kernel-cache-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.value = elf_fixture()
        self.item = {**cache.load_lock(), 'size': len(self.value), 'sha256': cache.sha(self.value)}
        self.stock = self.root / 'stock-ccache'
        self.stock.write_text('never executed by installer\n')
        self.stock.chmod(0o700)

    def toolchain(self, name='clang-toolchain'):
        root = self.root / name
        for folder in ('bin', 'lib/clang/19/include', 'lib64'):
            (root / folder).mkdir(parents=True, exist_ok=True)
        for name, value in {'bin/clang': b'compiler', 'bin/ld.lld': b'linker',
                            'lib/clang/19/include/stddef.h': b'resource',
                            'lib64/libLLVM.so': b'library'}.items():
            (root / name).write_bytes(value)
        return root

    def test_real_lock_is_immutable_and_matches_expected_elf_identity(self):
        spec = cache.load_lock()
        self.assertEqual(spec['commit'], 'f4e3a8084a7a5f8203e16ea177c8847c7ff517dc')
        self.assertEqual(spec['size'], 3849808)
        self.assertEqual(spec['sha256'], '40ecaea37903393fde418ad46fb8668546a04d6ed3b949059cbd4f1a1c148939')
        self.assertIn('/' + spec['commit'] + '/', spec['url'])

    def test_valid_pinned_elf_is_installed_atomically_without_execution(self):
        output = self.root / 'tools'
        with mock.patch.object(cache, 'load_lock', return_value=self.item), \
                mock.patch.object(cache, 'download', return_value=self.value), \
                mock.patch.object(cache, 'probe_binary') as probe, \
                mock.patch.object(subprocess, 'run') as execution:
            result = cache.install(output, stock=self.stock)
        execution.assert_not_called()
        probe.assert_called_once_with(output / 'ccache-real')
        self.assertEqual(result['engine'], 'ccache-ECS')
        self.assertEqual((output / 'ccache-real').read_bytes(), self.value)
        self.assertTrue((output / 'ccache').read_text().startswith('#!/bin/bash\n'))
        self.assertEqual(json.loads((output / 'cache-tool.json').read_text()), result)
        self.assertEqual([], list(output.glob('.ccache-real-*')))

    def test_size_hash_architecture_and_program_table_failures_use_stock_without_execution(self):
        invalid_arch = bytearray(self.value)
        struct.pack_into('<H', invalid_arch, 18, 183)
        invalid_table = bytearray(self.value)
        struct.pack_into('<Q', invalid_table, 32, 100000)
        for label, value, spec in [('size', self.value + b'x', self.item),
                                   ('hash', self.value[:-1] + b'x', self.item),
                                   ('arch', bytes(invalid_arch), {**self.item, 'sha256': cache.sha(invalid_arch)}),
                                   ('table', bytes(invalid_table), {**self.item, 'sha256': cache.sha(invalid_table)})]:
            with self.subTest(case=label), mock.patch.object(cache, 'load_lock', return_value=spec), \
                    mock.patch.object(cache, 'download', return_value=value), \
                    mock.patch.object(subprocess, 'run') as execution, contextlib.redirect_stderr(io.StringIO()):
                result = cache.install(self.root / label, stock=self.stock)
                self.assertEqual('stock-ccache', result['engine'])
                self.assertEqual(str(self.stock.resolve()), result['engine_path'])
                self.assertFalse((self.root / label / 'ccache-real').exists())
                execution.assert_not_called()

    def test_download_failure_or_absent_stock_disables_cache_without_disabling_compilation(self):
        with mock.patch.object(cache, 'download', side_effect=OSError('offline')), \
                contextlib.redirect_stderr(io.StringIO()):
            result = cache.install(self.root / 'offline', stock=self.root / 'absent')
        self.assertEqual('disabled', result['engine'])
        self.assertIn('exec "$compiler" "$@"', Path(result['program']).read_text())

    def test_full_compiler_linker_resource_and_library_contents_bind_identity(self):
        root = self.toolchain()
        expected = cache.compiler_identity(root / 'bin/clang', self.stock)
        for name in ('bin/clang', 'bin/ld.lld', 'lib/clang/19/include/stddef.h', 'lib64/libLLVM.so'):
            path = root / name
            old = path.read_bytes()
            path.write_bytes(b'x' * len(old))
            with self.subTest(file=name):
                self.assertNotEqual(expected, cache.compiler_identity(root / 'bin/clang', self.stock))
            path.write_bytes(old)
        self.stock.write_bytes(b'another cache engine')
        self.assertNotEqual(expected, cache.compiler_identity(root / 'bin/clang', self.stock))

    def test_identity_content_is_portable_and_has_no_installation_time_or_absolute_paths(self):
        left, right = self.toolchain('first'), self.toolchain('second')
        first = cache.compiler_identity(left / 'bin/clang', self.stock)
        second = cache.compiler_identity(right / 'bin/clang', self.stock)
        self.assertEqual(first, second)
        self.assertNotIn(str(self.root), json.dumps(first))
        self.assertNotIn('mtime', json.dumps(first))
        self.assertNotIn('run_id', json.dumps(first))
        self.assertLess(len(json.dumps(first)), 1024)

    def test_identity_rejects_system_wide_or_missing_toolchain_instead_of_hashing_usr(self):
        root = self.toolchain()
        with self.assertRaises((ValueError, OSError)):
            cache.compiler_identity(root / 'not-bin/clang', self.stock)
        for system in ('/usr/bin/cc', '/usr/bin/gcc'):
            candidate = Path(system)
            if candidate.is_file() and candidate.resolve().parent.parent.as_posix() == '/usr':
                with self.assertRaisesRegex(ValueError, 'system-wide'):
                    cache.compiler_identity(candidate, self.stock)

    def test_install_environment_outputs_use_wrapper_not_system_ccache(self):
        output, env, path = self.root / 'tools', self.root / 'env', self.root / 'path'
        with mock.patch.object(cache, 'load_lock', return_value=self.item), \
                mock.patch.object(cache, 'download', return_value=self.value), \
                mock.patch.object(cache, 'probe_binary'):
            self.assertEqual(0, cache.main(['install', '--output', str(output), '--github-env', str(env),
                                            '--github-path', str(path)]))
        values = dict(line.split('=', 1) for line in env.read_text().splitlines())
        self.assertEqual(str(output / 'ccache'), values['CCACHE_PROGRAM'])
        self.assertEqual(str(cache.ROOT / 'tools/kernel_compile_cache.py'), values['GKI_CACHE_TOOLS'])
        self.assertEqual('ccache-ECS', values['GKI_CACHE_ENGINE'])
        self.assertEqual(str(output) + '\n', path.read_text())

    def test_wrapper_forces_content_guards_and_hashes_full_autoconf_for_all_modes(self):
        wrapper = cache.shell_wrapper('/engine', Path('/helper'), Path('/identities'), '/python')
        self.assertIn('export CCACHE_COMPILERCHECK=content', wrapper)
        self.assertIn('export CCACHE_NOINODECACHE=1', wrapper)
        self.assertIn('export CCACHE_SLOPPINESS=include_file_ctime,include_file_mtime', wrapper)
        self.assertNotIn('file_stat_matches', wrapper)
        self.assertIn('"$cache_env" -u LD_PRELOAD -u FAKESTAT -u FAKETIME', wrapper)
        self.assertIn('"$cache_flock" -x 9', wrapper)
        self.assertIn('export CCACHE_EXTRAFILES="$extra"', wrapper)
        self.assertIn('if [ "${#configs[@]}" -eq 0 ]; then exec "$compiler" "$@"; fi', wrapper)
        self.assertIn('unset CCACHE_IS_KERNEL_COMPILING', wrapper)

    def test_bash_wrapper_forwards_flags_management_and_bypasses_unprovable_sources(self):
        shell = bash()
        if not shell:
            self.skipTest('Bash is required')
        root = self.toolchain()
        compiler = root / 'bin/clang'
        compiler.write_text('#!/usr/bin/sh\nprintf "direct:%s\\n" "$*"\n', newline='\n')
        compiler.chmod(0o700)
        engine = self.root / 'ecs'
        engine.write_text('#!/usr/bin/sh\nprintf "engine:%s\\n" "$*"\n', newline='\n')
        engine.chmod(0o700)
        ids = self.root / 'ids'
        ids.mkdir()
        wrapper = self.root / 'ccache'
        wrapper.write_text(cache.shell_wrapper(posix(engine), Path(posix(Path(cache.__file__))),
            Path(posix(ids)), posix(sys.executable)), encoding='utf-8', newline='\n')
        cases = [(['-s'], 'engine:-s'), ([posix(compiler), '-c', 'sample.c'], 'direct:-c sample.c'),
                 (['sloppiness=file_stat_matches', posix(compiler), '-c', 'sample.c'], 'direct:-c sample.c')]
        for args, expected in cases:
            with self.subTest(args=args):
                result = subprocess.run([shell, posix(wrapper), *args], capture_output=True, text=True,
                                        encoding='utf-8', cwd=self.root, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(expected, result.stdout.strip())

    def test_bounded_probe_and_truncated_http_failure_fall_back_to_guarded_stock(self):
        import http.client
        for error in (http.client.IncompleteRead(b'x', 10), subprocess.TimeoutExpired('ccache', 5),
                      OSError('missing ELF loader')):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(cache, 'load_lock', return_value=self.item), \
                    mock.patch.object(cache, 'download', side_effect=error), \
                    contextlib.redirect_stderr(io.StringIO()):
                result = cache.install(self.root / uuid.uuid4().hex, stock=self.stock)
                self.assertEqual('stock-ccache', result['engine'])
        with mock.patch.object(cache, 'load_lock', return_value=self.item), \
                mock.patch.object(cache, 'download', return_value=self.value), \
                mock.patch.object(cache, 'probe_binary', side_effect=OSError('incompatible GLIBC')), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual('stock-ccache', cache.install(self.root / 'no-loader', stock=self.stock)['engine'])

    def test_real_c_module_string_and_equal_size_config_changes_reach_guarded_engine(self):
        """Execute real C builds behind a recording engine, not Linux ECS.

        Native Windows/Linux execution proves semantic results, while the
        recording engine exposes the exact full-config/toolchain extra files
        ccache receives. A single-process flock stand-in is used only when
        Git for Windows lacks that POSIX utility; production uses real flock.
        """
        shell = bash()
        compiler = os.environ.get('BAKASU_NATIVE_CC') or shutil.which('cc') or shutil.which('clang')
        if not shell or not compiler:
            self.skipTest('A native C compiler and Bash are required')
        import shlex
        root = self.toolchain()
        native = root / 'bin/clang'
        invocation = [posix(compiler)]
        if Path(compiler).stem.lower() == 'zig':
            invocation.append('cc')
        native.write_text('#!/usr/bin/sh\nexec ' + ' '.join(shlex.quote(x) for x in invocation)
                          + ' "$@"\n', encoding='utf-8', newline='\n')
        native.chmod(0o700)
        # The tested subshell is single-process. The real Linux helper requires
        # flock; do not make production silently run without a lock.
        lock = root / 'bin/flock'
        lock.write_text('#!/usr/bin/sh\nexit 0\n', newline='\n')
        lock.chmod(0o700)
        # The bundled Git shell can lack sha256sum. This test-only equivalent
        # computes the same compiler-path key; production requires coreutils.
        checksum = root / 'bin/sha256sum'
        checksum.write_text('#!/usr/bin/sh\nexec ' + shlex.quote(posix(sys.executable)) + ' -c '
                            + shlex.quote("import sys,hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+'  -')")
                            + '\n', encoding='utf-8', newline='\n')
        checksum.chmod(0o700)
        engine, log, ids = self.root / 'ecs-record', self.root / 'engine.log', self.root / 'ids'
        engine.write_text('#!/usr/bin/sh\nprintf "%s\\n" "$CCACHE_EXTRAFILES" "$CCACHE_SLOPPINESS" '
                          '"$CCACHE_NOINODECACHE" "$CCACHE_COMPILERCHECK" "$CCACHE_IS_KERNEL_COMPILING" '
                          '> "$GUARD_TEST_LOG"\nexec "$@"\n', newline='\n')
        engine.chmod(0o700)
        ids.mkdir()
        wrapper = self.root / 'ccache'
        tool_paths = {'env': posix(Path(shell).parent / 'env.exe') if os.name == 'nt' else shutil.which('env'),
                      'readlink': posix(Path(shell).parent / 'readlink.exe') if os.name == 'nt' else shutil.which('readlink'),
                      'flock': posix(lock), 'sha256sum': posix(checksum)}
        wrapper.write_text(cache.shell_wrapper(posix(engine), Path(posix(Path(cache.__file__))),
            Path(posix(ids)), posix(sys.executable), tool_paths), encoding='utf-8', newline='\n')
        generated = self.root / 'include/generated'
        generated.mkdir(parents=True)
        autoconf = generated / 'autoconf.h'
        config_macros = '''#define __ARG_PLACEHOLDER_1 0,
#define __take_second_arg(__ignored, val, ...) val
#define __is_defined(x) ___is_defined(x)
#define ___is_defined(val) ____is_defined(__ARG_PLACEHOLDER_##val)
#define ____is_defined(arg1_or_junk) __take_second_arg(arg1_or_junk 1, 0)
#define IS_BUILTIN(option) __is_defined(option)
#define IS_MODULE(option) __is_defined(option##_MODULE)
#define IS_ENABLED(option) (IS_BUILTIN(option) || IS_MODULE(option))
'''
        cases = [('module', config_macros + 'int main(void) { return IS_ENABLED(CONFIG_FEATURE); }\n',
                  '#define CONFIG_OTHER 1\n', '#define CONFIG_OTHER 1\n#define CONFIG_FEATURE_MODULE 1\n'),
                 ('string', "int main(void) { return CONFIG_LABEL[3] == 't'; }\n",
                  '#define CONFIG_LABEL "ab one"\n', '#define CONFIG_LABEL "ab two"\n'),
                 ('equal_size', 'int main(void) { return CONFIG_VALUE == 2; }\n',
                  '#define CONFIG_VALUE 1\n', '#define CONFIG_VALUE 2\n')]
        # Deliberately omit every host utility from PATH, as Android's
        # HERMETIC_TOOLCHAIN does. The guard must use its bound absolute paths.
        environment = {**os.environ, 'GUARD_TEST_LOG': posix(log),
                       'PATH': str(root / 'bin'), 'CCACHE_IS_KERNEL_COMPILING': 'true'}
        fingerprints = []
        for name, source, before, after in cases:
            values, keys = [], []
            src = self.root / 'sample.c'
            src.write_text(source, encoding='utf-8', newline='\n')
            for index, config in enumerate((before, after)):
                autoconf.write_text(config, encoding='utf-8', newline='\n')
                executable = self.root / ('sample-' + name + '-' + str(index) + ('.exe' if os.name == 'nt' else ''))
                result = subprocess.run([shell, posix(wrapper), posix(native), '-Werror', '-include',
                    posix(autoconf), posix(src), '-o', posix(executable)], env=environment,
                    capture_output=True, text=True, encoding='utf-8', cwd=self.root, timeout=90)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(log.is_file(), 'Compiler unexpectedly bypassed the verified-cache branch')
                lines = log.read_text().splitlines()
                descriptors = list(ids.glob('*.json'))
                self.assertEqual(1, len(descriptors))
                mode = json.loads(descriptors[0].read_text())['ecs_kernel_mode']
                self.assertEqual(['include_file_ctime,include_file_mtime', '1', 'content',
                                  'true' if mode else ''], lines[1:])
                extra = []
                for value in lines[0].split(':'):
                    if os.name == 'nt' and len(value) > 3 and value[0] == '/' and value[2] == '/':
                        value = value[1].upper() + ':' + value[2:]
                    extra.append(Path(value))
                self.assertEqual(2, len(extra))
                self.assertEqual(autoconf.resolve(), extra[1].resolve())
                keys.append(hashlib.sha256(b''.join(path.read_bytes() for path in extra)).hexdigest())
                values.append(subprocess.run([str(executable)], capture_output=True).returncode)
            with self.subTest(case=name):
                self.assertEqual([0, 1], values)
                self.assertNotEqual(keys[0], keys[1])
            fingerprints.append(keys)
        self.assertEqual(1, len(list(ids.glob('*.json'))), 'The toolchain inventory should be computed once per compiler')
        descriptor = next(ids.glob('*.json'))
        document = json.loads(descriptor.read_text())
        for major, kernel_mode in ((14, False), (18, True)):
            # Classification is exercised independently below. Here verify the
            # actual shell honors both cached decisions despite an inherited
            # kernel-mode=true from the surrounding CCTV recipe.
            document['ecs_kernel_mode'] = kernel_mode
            descriptor.write_text(json.dumps(document, separators=(',', ':')), encoding='utf-8')
            result = subprocess.run([shell, posix(wrapper), posix(native), '-include', posix(autoconf),
                posix(src), '-o', posix(executable)], env=environment, capture_output=True, text=True,
                encoding='utf-8', cwd=self.root, timeout=90)
            with self.subTest(clang_major=major):
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual('true' if kernel_mode else '', log.read_text().splitlines()[-1])

    def test_only_recognized_clang_18_or_newer_enables_ecs_time_shortcuts(self):
        for version, expected in ((b'Android clang version 14.0.7\n', False),
                                  (b'clang version 17.0.0\n', False),
                                  (b'clang version 18.0.0\n', True),
                                  (b'clang version 19.1.2\n', True),
                                  (b'gcc version 14.2\n', False)):
            with self.subTest(version=version), mock.patch.object(cache.subprocess, 'run',
                    return_value=subprocess.CompletedProcess(['clang'], 0, stdout=version)):
                self.assertEqual(expected, cache.modern_clang(Path('/toolchain/bin/clang')))
        for error in (OSError('not executable'), subprocess.TimeoutExpired('clang', 5)):
            with self.subTest(error=error), mock.patch.object(cache.subprocess, 'run', side_effect=error):
                self.assertFalse(cache.modern_clang(Path('/toolchain/bin/clang')))


if __name__ == '__main__':
    unittest.main()
