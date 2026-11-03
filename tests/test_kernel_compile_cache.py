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

    def shell_policy_fixture(self, name, *, modern=True, pinned=True):
        """Exercise the real guard with a recording engine and native fallback."""
        import shlex
        shell = bash()
        if not shell:
            self.skipTest('Bash is required')
        directory = self.root / name
        root = self.toolchain(name + '/toolchain')
        compiler = root / 'bin/clang'
        compiler.write_text('#!/usr/bin/sh\nprintf "native compiler\\n"\n', newline='\n')
        compiler.chmod(0o700)
        engine, log = directory / 'engine', directory / 'engine.log'
        engine.write_text('#!/usr/bin/sh\nprintf "%s\\n" "$CCACHE_COMPILERCHECK" '
                          '"${CCACHE_IS_KERNEL_COMPILING:-}" "$CCACHE_EXTRAFILES" '
                          '"$SOURCE_DATE_EPOCH" "$CCACHE_NOINODECACHE" > "$GUARD_TEST_LOG"\n'
                          'printf "recording engine\\n"\n', newline='\n')
        engine.chmod(0o700)
        utilities = directory / 'utilities'
        utilities.mkdir()
        lock, checksum = utilities / 'flock', utilities / 'sha256sum'
        lock.write_text('#!/usr/bin/sh\nexit 0\n', newline='\n')
        checksum.write_text('#!/usr/bin/sh\nexec ' + shlex.quote(posix(sys.executable)) + ' -c '
            + shlex.quote("import sys,hashlib; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()+'  -')")
            + '\n', encoding='utf-8', newline='\n')
        lock.chmod(0o700); checksum.chmod(0o700)
        tools = {'env': posix(Path(shell).parent / 'env.exe') if os.name == 'nt' else shutil.which('env'),
                 'readlink': posix(Path(shell).parent / 'readlink.exe') if os.name == 'nt' else shutil.which('readlink'),
                 'flock': posix(lock), 'sha256sum': posix(checksum)}
        identities = directory / 'ids'
        identities.mkdir()
        descriptor = identities / (cache.sha(posix(compiler).encode()) + '.json')
        with mock.patch.object(cache, 'modern_clang', return_value=modern):
            identity = cache.compiler_identity(compiler, engine)
        descriptor.write_text(json.dumps(identity, sort_keys=True, separators=(',', ':')) + '\n')
        configs = [directory / 'include/generated/autoconf.h', directory / 'other/include/generated/autoconf.h']
        for index, config in enumerate(configs):
            config.parent.mkdir(parents=True)
            config.write_text('#define CONFIG_VALUE ' + str(index) + '\n')
        inherited = directory / 'inherited.guard'
        inherited.write_bytes(b'keep inherited extra input')
        wrapper = directory / 'ccache'
        wrapper.write_text(cache.shell_wrapper(posix(engine), Path('/not-invoked'), Path(posix(identities)),
            posix(sys.executable), tools, pinned_ecs_sha256=identity['engine_sha256'] if pinned else ''),
            encoding='utf-8', newline='\n')
        environment = {**os.environ, 'GUARD_TEST_LOG': posix(log), 'CCACHE_COMPILERCHECK': 'none',
                       'CCACHE_IS_KERNEL_COMPILING': 'true', 'CCACHE_EXTRAFILES': posix(inherited)}
        def run(*extra):
            return subprocess.run([shell, posix(wrapper), posix(compiler), '-include', posix(configs[0]),
                '-I' + posix(configs[1].parent.parent), '-c', 'sample.c', *extra], env=environment,
                capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=directory, timeout=30)
        return {'run': run, 'directory': directory, 'descriptor': descriptor, 'identity': identity,
                'compiler': compiler, 'configs': configs, 'inherited': inherited, 'log': log}

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
        self.assertIn('pinned_ecs_sha256=' + self.item['sha256'] + '\n', (output / 'ccache').read_text())
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
                self.assertIn("pinned_ecs_sha256=''\n", Path(result['program']).read_text())
                execution.assert_not_called()

    def test_download_failure_or_absent_stock_disables_cache_without_disabling_compilation(self):
        with mock.patch.object(cache, 'download', side_effect=OSError('offline')), \
                contextlib.redirect_stderr(io.StringIO()):
            result = cache.install(self.root / 'offline', stock=self.root / 'absent')
        self.assertEqual('disabled', result['engine'])
        self.assertIn("pinned_ecs_sha256=''\n", Path(result['program']).read_text())
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

    @unittest.skipUnless(os.name == 'posix', 'Real compiler-package symlink verification requires POSIX')
    def test_official_clang_dangling_compatibility_links_are_bound_as_absent_inputs(self):
        root = self.toolchain()
        target = root / 'lib/x86_64-unknown-linux-gnu/libc++.so.1.0'
        target.parent.mkdir()
        link = root / 'lib/libc++.so.1.0'
        link.symlink_to('x86_64-unknown-linux-gnu/libc++.so.1.0')
        missing = cache.compiler_identity(root / 'bin/clang', self.stock)
        self.assertEqual(missing, cache.compiler_identity(root / 'bin/clang', self.stock))
        # The absent link is retained, so removing it or changing even its
        # still-missing target must change the complete toolchain fingerprint.
        link.unlink()
        without_link = cache.compiler_identity(root / 'bin/clang', self.stock)
        self.assertNotEqual(missing, without_link)
        link.symlink_to('x86_64-unknown-linux-gnu/another-missing-library')
        self.assertNotEqual(missing, cache.compiler_identity(root / 'bin/clang', self.stock))
        link.unlink()
        link.symlink_to('x86_64-unknown-linux-gnu/libc++.so.1.0')
        target.write_bytes(b'complete real library')
        present = cache.compiler_identity(root / 'bin/clang', self.stock)
        self.assertNotEqual(missing, present)
        target.write_bytes(b'changed real library!')
        self.assertNotEqual(present, cache.compiler_identity(root / 'bin/clang', self.stock))
        # The existing directory-cycle rejection must remain effective.
        (root / 'lib/cycle').symlink_to('.', target_is_directory=True)
        with self.assertRaises(ValueError):
            cache.compiler_identity(root / 'bin/clang', self.stock)

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
        self.assertIn('if [ "${#configs[@]}" -eq 0 ]; then cache_note config-unavailable; exec "$compiler" "$@"; fi', wrapper)
        self.assertIn('unset CCACHE_IS_KERNEL_COMPILING', wrapper)
        import re
        self.assertEqual({'cache-unavailable', 'compiler-unresolved', 'utility-unavailable',
                          'response-arguments', 'plugin-arguments', 'config-unavailable',
                          'descriptor-unavailable', 'engine-entered'},
                         set(re.findall(r'cache_note ([a-z-]+)', wrapper)))

    def test_modern_verified_ecs_binds_live_compiler_full_inventory_and_every_config_before_none(self):
        fixture = self.shell_policy_fixture('modern-pinned')
        result = fixture['run']()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('recording engine', result.stdout.strip())
        lines = fixture['log'].read_text().splitlines()
        expected = [fixture['descriptor'], fixture['compiler'], *fixture['configs'], fixture['inherited']]
        self.assertEqual(['none', 'true', ':'.join(posix(path) for path in expected), cache.EPOCH, '1'], lines)
        # The descriptor stays a per-job snapshot, while the actual compiler
        # file remains an extra input even after same-size/mtime changes.
        original_descriptor = fixture['descriptor'].read_bytes()
        old = fixture['compiler'].read_bytes()
        before = fixture['compiler'].stat()
        fixture['compiler'].write_bytes(old.replace(b'native compiler', b'native changed!'))
        os.utime(fixture['compiler'], ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(len(old), fixture['compiler'].stat().st_size)
        self.assertEqual(0, fixture['run']().returncode)
        self.assertEqual(original_descriptor, fixture['descriptor'].read_bytes())
        self.assertEqual(lines, fixture['log'].read_text().splitlines())
        self.assertNotEqual(cache.sha(old), cache.file_digest(fixture['compiler']))

    def test_old_unknown_and_stock_compilers_keep_content_even_with_an_inherited_none_override(self):
        for name, modern, pinned in (('old-pinned', False, True), ('modern-stock', True, False),
                                     ('unknown-stock', False, False)):
            with self.subTest(name=name):
                fixture = self.shell_policy_fixture(name, modern=modern, pinned=pinned)
                result = fixture['run']('-fplugin=legacy-plugin.so')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('recording engine', result.stdout.strip())
                lines = fixture['log'].read_text().splitlines()
                expected = [fixture['descriptor'], *fixture['configs'], fixture['inherited']]
                self.assertEqual(['content', 'true' if modern else '',
                                  ':'.join(posix(path) for path in expected), cache.EPOCH, '1'], lines)

    def test_modern_pinned_policy_bypasses_missing_or_invalid_proofs_and_external_plugins(self):
        cases = ('missing-config', 'missing-descriptor', 'disabled-descriptor', 'incomplete-descriptor',
                 'wrong-engine', 'wrong-compiler', '-fplugin=outside.so', '-fpass-plugin=outside.so', '-load=outside.so',
                 '--load=outside.so', '-mllvm=-load=outside.so', '-mllvm=--load-pass-plugin=outside.so',
                 '-Xclang=-load', '@hidden-options')
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                fixture = self.shell_policy_fixture('bypass-' + str(index))
                extra = []
                if case == 'missing-config':
                    for config in fixture['configs']: config.unlink()
                elif case == 'missing-descriptor':
                    fixture['descriptor'].unlink()
                elif case == 'disabled-descriptor':
                    fixture['descriptor'].with_suffix('.disabled').touch()
                elif case in ('incomplete-descriptor', 'wrong-engine', 'wrong-compiler'):
                    identity = fixture['identity']
                    if case == 'incomplete-descriptor': identity.pop('toolchain_sha256')
                    elif case == 'wrong-compiler': identity['compiler'] = 'bin/another-clang'
                    else: identity['engine_sha256'] = '0' * 64
                    fixture['descriptor'].write_text(json.dumps(identity, sort_keys=True, separators=(',', ':')))
                else:
                    extra = [case]
                result = fixture['run'](*extra)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('native compiler', result.stdout.strip())
                self.assertFalse(fixture['log'].exists())
                reason = ('plugin-arguments' if case.startswith('-') else
                          'response-arguments' if case.startswith('@') else
                          'config-unavailable' if case == 'missing-config' else 'descriptor-unavailable')
                self.assertIn('Kernel cache guard: ' + reason, result.stderr)

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

    def test_bypass_diagnostics_are_once_per_reason_and_do_not_expose_inputs(self):
        shell = bash()
        if not shell:
            self.skipTest('Bash required')
        compiler = self.root / 'native-compiler'
        compiler.write_text('#!/usr/bin/sh\nprintf "native compile\\n"\n', newline='\n')
        compiler.chmod(0o700)
        engine = self.root / 'engine'
        engine.write_text('#!/usr/bin/sh\nprintf "engine compile\\n"\n', newline='\n')
        engine.chmod(0o700)
        # These utilities are only tested for executability: every case below
        # safely exits before any compiler identity operation can call them.
        utility = self.root / 'utility'
        utility.write_text('#!/usr/bin/sh\nexit 0\n', newline='\n')
        utility.chmod(0o700)
        ready = {name: posix(utility) for name in ('env', 'readlink', 'flock', 'sha256sum')}
        cases = (
            ('cache-unavailable', '', ready, [posix(compiler), '-c', 'private-source.c']),
            ('utility-unavailable', posix(engine), {**ready, 'flock': ''}, [posix(compiler), '-c', 'private-source.c']),
            ('response-arguments', posix(engine), ready, [posix(compiler), '@private-response-options']),
            ('config-unavailable', posix(engine), ready, [posix(compiler), '-c', 'private-source.c']),
        )
        for reason, program, tools, arguments in cases:
            with self.subTest(reason=reason):
                identities = self.root / ('diagnostics-' + reason)
                identities.mkdir()
                wrapper = self.root / ('wrapper-' + reason)
                wrapper.write_text(cache.shell_wrapper(program, Path('/not-invoked'), Path(posix(identities)),
                    posix(sys.executable), tools), encoding='utf-8', newline='\n')
                command = [shell, posix(wrapper), *arguments]
                first = subprocess.run(command, capture_output=True, text=True, cwd=self.root, timeout=20)
                self.assertEqual(0, first.returncode, first.stdout + first.stderr)
                self.assertEqual('native compile', first.stdout.strip())
                self.assertEqual('Kernel cache guard: ' + reason, first.stderr.strip())
                self.assertNotIn('private', first.stderr)
                self.assertNotIn(str(self.root), first.stderr)
                second = subprocess.run(command, capture_output=True, text=True, cwd=self.root, timeout=20)
                self.assertEqual(0, second.returncode, second.stdout + second.stderr)
                self.assertEqual('native compile', second.stdout.strip())
                self.assertEqual('', second.stderr)

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
        if os.name == 'posix':
            # The real host driver still needs its assembler and linker. Bind
            # only those two programs into the otherwise restricted test PATH;
            # none of the guard's host utilities become discoverable there.
            for program in ('as', 'ld'):
                resolved = shutil.which(program)
                self.assertIsNotNone(resolved, 'Real C verification requires ' + program)
                (root / 'bin' / program).symlink_to(Path(resolved).resolve())
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
                  '#define CONFIG_VALUE 1\n', '#define CONFIG_VALUE 2\n'),
                 ('kleaf_hint', '#include ' + json.dumps(autoconf.as_posix())
                  + '\nint main(void) { return CONFIG_VALUE == 2; }\n',
                  '#define CONFIG_VALUE 1\n', '#define CONFIG_VALUE 2\n')]
        # Deliberately omit host shell utilities from PATH, as Android's
        # HERMETIC_TOOLCHAIN does. Only compiler helpers above are provided;
        # the guard must still use its bound absolute utility paths.
        environment = {**os.environ, 'GUARD_TEST_LOG': posix(log),
                       'PATH': str(root / 'bin'), 'CCACHE_IS_KERNEL_COMPILING': 'true'}
        fingerprints = []
        hidden_cwd = self.root / 'compiler-cwd'
        hidden_cwd.mkdir()
        for name, source, before, after in cases:
            values, keys = [], []
            src = self.root / 'sample.c'
            src.write_text(source, encoding='utf-8', newline='\n')
            for index, config in enumerate((before, after)):
                autoconf.write_text(config, encoding='utf-8', newline='\n')
                executable = self.root / ('sample-' + name + '-' + str(index) + ('.exe' if os.name == 'nt' else ''))
                hint_case = name == 'kleaf_hint'
                selected_environment = {**environment}
                selected_environment.pop('GKI_CCACHE_AUTOCONF', None)
                if hint_case:
                    selected_environment['GKI_CCACHE_AUTOCONF'] = posix(autoconf)
                flags = [] if hint_case else ['-include', posix(autoconf)]
                result = subprocess.run([shell, posix(wrapper), posix(native), '-Werror', *flags,
                    posix(src), '-o', posix(executable)], env=selected_environment,
                    capture_output=True, text=True, encoding='utf-8',
                    cwd=hidden_cwd if hint_case else self.root, timeout=90)
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
