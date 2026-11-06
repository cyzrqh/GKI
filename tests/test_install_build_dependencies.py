"""Replay dependency recovery without sudo, apt, or network access.

The real helper runs unchanged. Only the Ubuntu identity and system commands
are replaced; temporary source files and their cleanup use the real filesystem.
"""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'tools/install_build_dependencies.sh'
GKI_PACKAGES = {'ccache', 'python3', 'python3-cryptography', 'git', 'curl',
                'build-essential', 'libssl-dev', 'bison', 'flex', 'libelf-dev',
                'dwarves', 'lz4'}
OPLUS_PACKAGES = {'aria2', 'bc', 'bison', 'flex', 'make', 'gcc', 'g++', 'binutils',
                  'python-is-python3', 'libssl-dev', 'libelf-dev', 'libdw-dev',
                  'zlib1g-dev', 'ccache', 'lz4', 'zstd', 'xz-utils', 'zip',
                  'unzip', 'patch', 'curl', 'wget', 'rsync'}


def bash_path():
    if shell := os.environ.get('GKI_TEST_BASH') or shutil.which('bash'):
        return shell
    if git := shutil.which('git'):
        for name in ('bash.exe', 'sh.exe'):
            candidate = Path(git).parent.parent / 'usr/bin' / name
            if candidate.is_file():
                return str(candidate)
    return None


BASH = bash_path()

STUBS = r'''
set +o posix
ready=0
attempts=0
dpkg-query() {
  if ((ready)) || [[ "$TEST_MISSING" != '*' && "|$TEST_MISSING|" != *"|${@: -1}|"* ]]; then
    printf installed
  else
    return 1
  fi
}
dpkg() { [[ "$*" == --print-architecture ]] || return 81; printf '%s' "$TEST_ARCH"; }
chmod() { [[ "$#" == 2 && "$1" == 755 && "$2" == "$PWD"/gki-build-deps.* ]]; }
function .() {
  [[ "$1" == /etc/os-release ]] || return 82
  ID="$TEST_DISTRO"
  VERSION_CODENAME="$TEST_CODENAME"
}
sudo() {
  case "$1" in
    timeout) shift; timeout "$@" ;;
    rm)
      # Refuse any mutation outside this test's single generated directory.
      [[ "${@: -1}" == "$PWD"/gki-build-deps.* ]] || return 83
      command "$@"
      ;;
    *) printf 'unexpected privileged operation: %s\n' "$*" >&2; return 84 ;;
  esac
}
timeout() {
  printf '%s\n' "$*" >> "$PWD/apt-calls"
  [[ "$1" == --kill-after=10s && "$3" == env && "$4" == DEBIAN_FRONTEND=noninteractive && "$5" == apt-get ]] || return 85
  if [[ " ${*: -1} " == ' update ' ]]; then
    [[ "$2" == 90s ]] || return 86
    local option source_file='' list_dir='' source_dir=''
    for option in "$@"; do
      case "$option" in
        Dir::Etc::sourcelist=*) source_file="${option#*=}" ;;
        Dir::Etc::sourceparts=*) source_dir="${option#*=}" ;;
        Dir::State::lists=*) list_dir="${option#*=}" ;;
      esac
    done
    [[ "$source_file" == "$PWD"/gki-build-deps.*/ubuntu.sources && -f "$source_file" ]] || return 87
    [[ "$list_dir" == "${source_file%/*}/lists" && -d "$list_dir/partial" ]] || return 88
    [[ "$source_dir" == "${source_file%/*}/sources.list.d" && -d "$source_dir" ]] || return 89
    command cat "$source_file" > "$PWD/used-sources"
    printf '%s\n' "$list_dir" > "$PWD/used-lists"
    return "$TEST_UPDATE_STATUS"
  fi
  [[ "$2" == 180s && " $* " == *' install -y --no-upgrade --no-install-recommends '* ]] || return 90
  attempts=$((attempts + 1))
  if ((attempts == 1 && TEST_FIRST_STATUS != 0)); then return "$TEST_FIRST_STATUS"; fi
  if ((attempts > 1 && TEST_FINAL_STATUS != 0)); then return "$TEST_FINAL_STATUS"; fi
  if [[ "$TEST_LEAVE_MISSING" != true ]]; then ready=1; fi
}
export RUNNER_TEMP="$PWD"
'''


@unittest.skipUnless(BASH, 'Bash is required for dependency recovery tests')
class InstallBuildDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / 'tests' / ('.dependency-installer-' + uuid.uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def run_case(self, profile='gki', **overrides):
        case = self.directory / uuid.uuid4().hex
        case.mkdir()
        env = {**os.environ, 'PATH': str(Path(BASH).parent) + os.pathsep + os.environ.get('PATH', ''),
               'TEST_MISSING': '*', 'TEST_FIRST_STATUS': '0', 'TEST_FINAL_STATUS': '0',
               'TEST_UPDATE_STATUS': '0', 'TEST_LEAVE_MISSING': 'false',
               'TEST_DISTRO': 'ubuntu', 'TEST_CODENAME': 'noble', 'TEST_ARCH': 'amd64',
               **{key: str(value) for key, value in overrides.items()}}
        script = STUBS + '\nset -- ' + shlex.quote(profile) + '\n' + INSTALLER.read_text('utf-8')
        result = subprocess.run([BASH, '--noprofile', '--norc', '-c', script], cwd=case,
                                env=env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        calls = (case / 'apt-calls').read_text().splitlines() if (case / 'apt-calls').exists() else []
        self.assertEqual([], list(case.glob('gki-build-deps.*')), 'Temporary apt state was not removed')
        return result, calls, case

    def assert_success(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('all required packages verified', result.stdout)

    def test_preinstalled_runner_never_calls_apt_or_creates_sources(self):
        for profile in ('gki', 'oplus'):
            with self.subTest(profile=profile):
                result, calls, case = self.run_case(profile, TEST_MISSING='')
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn('already installed', result.stdout)
                self.assertEqual([], calls)
                self.assertFalse((case / 'used-sources').exists())

    def test_each_profile_retains_its_complete_original_dependency_set(self):
        for profile, expected in (('gki', GKI_PACKAGES), ('oplus', OPLUS_PACKAGES)):
            with self.subTest(profile=profile):
                result, calls, _ = self.run_case(profile)
                self.assert_success(result)
                self.assertEqual(1, len(calls))
                packages = calls[0].split('--no-install-recommends ', 1)[1].split()
                self.assertEqual(expected, set(packages))
                self.assertEqual(len(expected), len(packages))

    def test_only_missing_packages_are_installed_using_runner_indexes_first(self):
        for profile in ('gki', 'oplus'):
            with self.subTest(profile=profile):
                result, calls, case = self.run_case(profile, TEST_MISSING='ccache|lz4')
                self.assert_success(result)
                self.assertEqual(1, len(calls))
                self.assertEqual({'ccache', 'lz4'}, set(calls[0].split('--no-install-recommends ', 1)[1].split()))
                self.assertNotIn('Dir::Etc::sourcelist=', calls[0])
                self.assertFalse((case / 'used-sources').exists())

    def test_stale_runner_index_recovers_with_isolated_signed_official_https_sources(self):
        for profile in ('gki', 'oplus'):
            with self.subTest(profile=profile):
                result, calls, case = self.run_case(profile, TEST_FIRST_STATUS=100)
                self.assert_success(result)
                self.assertEqual(3, len(calls))
                self.assertIn('90s', calls[1])
                self.assertIn('APT::Update::Error-Mode=any', calls[1])
                self.assertIn('Acquire::Languages=none', calls[1])
                self.assertIn('Dir::Etc::sourcelist=', calls[2])
                sources = (case / 'used-sources').read_text()
                self.assertIn('https://archive.ubuntu.com/ubuntu', sources)
                self.assertIn('https://security.ubuntu.com/ubuntu', sources)
                self.assertIn('Suites: noble noble-updates noble-backports', sources)
                self.assertIn('Suites: noble-security', sources)
                self.assertIn('Architectures: amd64', sources)
                self.assertEqual(2, sources.count('Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg'))
                for forbidden in ('azure', 'mirrorlist', 'microsoft', 'chrome', 'http:', 'Trusted:', 'trusted=yes'):
                    self.assertNotIn(forbidden, sources)

    def test_refresh_timeout_can_only_succeed_after_a_successful_final_install(self):
        for profile in ('gki', 'oplus'):
            with self.subTest(profile=profile):
                result, calls, _ = self.run_case(profile, TEST_FIRST_STATUS=100, TEST_UPDATE_STATUS=124)
                self.assert_success(result)
                self.assertEqual(3, len(calls))
                self.assertIn('::warning::', result.stdout)

    def test_install_failure_and_timeout_are_not_hidden_by_recovery_or_cleanup(self):
        for profile in ('gki', 'oplus'):
            for status in (100, 124, 137):
                with self.subTest(profile=profile, status=status):
                    result, calls, _ = self.run_case(profile, TEST_FIRST_STATUS=124,
                        TEST_UPDATE_STATUS=124, TEST_FINAL_STATUS=status)
                    self.assertEqual(status, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(3, len(calls))
                    self.assertNotIn('all required packages verified', result.stdout)

    def test_apt_success_cannot_hide_a_missing_or_unconfigured_dependency(self):
        for profile in ('gki', 'oplus'):
            with self.subTest(profile=profile):
                result, calls, _ = self.run_case(profile, TEST_LEAVE_MISSING='true')
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertEqual(1, len(calls))
                self.assertIn('dependencies are still missing', result.stderr)
                self.assertNotIn('all required packages verified', result.stdout)

    def test_arm_runner_uses_the_signed_official_ports_archive(self):
        result, _, case = self.run_case(TEST_ARCH='arm64', TEST_FIRST_STATUS=100)
        self.assert_success(result)
        sources = (case / 'used-sources').read_text()
        self.assertEqual(2, sources.count('https://ports.ubuntu.com/ubuntu-ports'))
        self.assertIn('Architectures: arm64', sources)

    def test_invalid_profile_or_recovery_identity_fails_before_index_refresh(self):
        result, calls, _ = self.run_case('invalid')
        self.assertEqual(2, result.returncode)
        self.assertEqual([], calls)
        for invalid in ({'TEST_DISTRO': 'debian'}, {'TEST_CODENAME': ''},
                        {'TEST_CODENAME': 'noble;trusted=yes'}, {'TEST_ARCH': '../amd64'}):
            with self.subTest(invalid=invalid):
                result, calls, _ = self.run_case(TEST_FIRST_STATUS=100, **invalid)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(1, len(calls))
                self.assertNotIn('all required packages verified', result.stdout)


if __name__ == '__main__':
    unittest.main()
