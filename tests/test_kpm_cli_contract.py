"""Compare the rendered page with the real production Rust/Clap command enum."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import uuid

from additional_manager_kpm import template_files
from additional_manager_kpm_ui import render_kpm_templates


ROOT = Path(__file__).resolve().parents[1]
CARGO = os.environ.get('GKI_TEST_CARGO') or shutil.which('cargo')


def production_cli() -> str:
    rust = template_files()['userspace/ksud/src/android/kpm.rs']
    start = rust.index('#[derive(Args, Debug)]')
    return rust[start:rust.index('\nfn check_result', start)]


def frontend_calls(screen: str) -> set[str]:
    calls = set(re.findall(r'(?:jsonCommand|command)\("([a-z]+)"', screen))
    conditional = re.findall(r'command\(if \(embed\) "([a-z]+)" else "([a-z]+)"', screen)
    calls.update(value for pair in conditional for value in pair)
    return calls


class KpmCliContractTests(unittest.TestCase):
    def test_every_frontend_command_exists_in_the_production_native_enum(self):
        rust = production_cli()
        native = {name.lower() for name in re.findall(r'^    ([A-Z]\w*)\s*\{', rust, re.M)}
        expected = {'supported', 'version', 'list', 'installed', 'load', 'install',
                    'info', 'remove', 'unload', 'control'}
        self.assertIn('remove', native)
        self.assertNotIn('uninstall', native)
        for shell in ('cached', 'new', 'bakasu'):
            with self.subTest(shell=shell):
                screen = render_kpm_templates('example.manager', shell)['GkiKpmScreen.kt']
                calls = frontend_calls(screen)
                self.assertEqual(expected, calls)
                self.assertTrue(calls <= native, 'Frontend calls unavailable native commands: ' + str(calls - native))
                positional = re.search(r'val positional = setOf\(([^\n]+)\)', screen)[1]
                literal_commands = set(re.findall(r'"([a-z]+)"', positional))
                self.assertEqual({'load', 'install', 'info', 'remove', 'unload', 'control'}, literal_commands)
                self.assertIn('listOf(args.first(), "--") + args.drop(1)', screen)

    def test_existing_json_responses_match_the_frontend_abi_and_array_contract(self):
        rust = template_files()['userspace/ksud/src/android/kpm.rs']
        self.assertIn('const BRIDGE_ABI: u32 = 2;', rust)
        self.assertIn('{"supported": supported, "abi": BRIDGE_ABI}', rust)
        self.assertIn('{"version": version, "abi": BRIDGE_ABI}', rust)
        self.assertEqual(2, rust.count('serde_json::to_string(&names)?'))
        self.assertIn('KpmCommand::Remove { name }', rust)
        self.assertIn('kpm_store::remove(Path::new(KPM_DIR), &name)?', rust)

    @unittest.skipUnless(CARGO, 'Cargo is required to execute the production Clap parser on the host')
    def test_actual_clap_parser_accepts_the_frontend_argv_and_rejects_the_old_spelling(self):
        work = ROOT / 'tests' / ('.kpm-cli-contract-' + uuid.uuid4().hex)
        (work / 'src').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, work)
        (work / 'Cargo.toml').write_text('''[package]
name = "gki-kpm-cli-contract"
version = "0.1.0"
edition = "2021"
publish = false
[dependencies]
clap = { version = "=4.6.7", default-features = false, features = ["derive", "std", "help", "usage", "error-context", "suggestions"] }
''', encoding='utf-8')
        if vendor := os.environ.get('GKI_TEST_KPM_CLI_VENDOR'):
            (work / '.cargo').mkdir()
            (work / '.cargo/config.toml').write_text('[source.crates-io]\nreplace-with = "vendored"\n'
                '[source.vendored]\ndirectory = ' + json.dumps(Path(vendor).as_posix()) + '\n', encoding='utf-8')
        # The exact Args/enum definitions come from the production patch, not a
        # duplicate command fixture. This host binary only parses; it cannot do IOCTLs.
        (work / 'src/main.rs').write_text('use clap::{Args, Parser, Subcommand};\n' + production_cli() + '''
#[derive(Parser)]
struct HostCli { #[command(flatten)] kpm: KpmArgs }
fn main() { let _ = HostCli::parse(); }
''', encoding='utf-8')
        built = subprocess.run([CARGO, 'build', '--offline', '--quiet'], cwd=work,
            capture_output=True, text=True, encoding='utf-8', timeout=180)
        self.assertEqual(0, built.returncode, built.stdout + built.stderr)
        binary = work / 'target/debug' / ('gki-kpm-cli-contract.exe' if os.name == 'nt' else 'gki-kpm-cli-contract')
        scenarios = {
            'supported': ['--json'], 'version': ['--json'], 'list': ['--json'], 'installed': ['--json'],
            'load': ['--', '/literal module.kpm', ''], 'install': ['--', '/literal module.kpm', ''],
            'info': ['--', '--literal-name'], 'remove': ['--', '--literal-name'],
            'unload': ['--', '--literal-name'], 'control': ['--', '--literal-name', 'literal;$(argument)'],
        }
        screen = render_kpm_templates('example.manager', 'cached')['GkiKpmScreen.kt']
        self.assertEqual(frontend_calls(screen), set(scenarios))
        for command in sorted(frontend_calls(screen)):
            with self.subTest(command=command):
                result = subprocess.run([str(binary), command, *scenarios[command]],
                    capture_output=True, text=True, encoding='utf-8', timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
        rejected = subprocess.run([str(binary), 'uninstall', '--', 'literal-name'],
            capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn('unrecognized subcommand', rejected.stderr)


if __name__ == '__main__':
    unittest.main()
