"""Check the requested KPM tab against the real official navigation source."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import restore_bakasu_kpm as port

BASE = 'manager/app/src/main/java/org/bakasu/bakasu/'
NAVIGATION = BASE + 'ui/screen/BottomBarDestination.kt'
FIXTURE = ROOT / 'tests/fixtures/bakasu-kpm-navigation'


def sections(raw):
    result = {}
    for chunk in re.split(rb'(?=^diff --git )', raw, flags=re.M):
        if not chunk:
            continue
        name = chunk.split(b'\n', 1)[0].decode().split(' b/', 1)[1]
        result[name] = chunk
    return result


def added_file(chunk):
    return b''.join(line[1:] for line in chunk.splitlines(keepends=True)
                    if line.startswith(b'+') and not line.startswith(b'+++'))


class BakaKpmNavigationTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.patch = port.load_port_manifest()
        self.inventory = {item['path']: item for item in self.manifest['files']}
        self.sections = sections(self.patch)
        self.directory = ROOT / 'tests' / ('.bakasu-nav-' + uuid.uuid4().hex)
        self.directory.mkdir()
        subprocess.run(['git', 'init', '--quiet', str(self.directory)], check=True,
                       capture_output=True, timeout=30)
        self.addCleanup(shutil.rmtree, self.directory)

    def navigation(self):
        native = bytes.fromhex((FIXTURE / 'BottomBarDestination.kt.hex').read_text('ascii'))
        source = self.directory / NAVIGATION
        source.parent.mkdir(parents=True)
        source.write_bytes(native)
        provenance = json.loads((FIXTURE / 'provenance.json').read_bytes())
        self.assertEqual(provenance['path'], NAVIGATION)
        self.assertEqual(port.AUDITED_REF, provenance['commit'])
        self.assertEqual(provenance['sha256'], hashlib.sha256(native).hexdigest())
        self.assertEqual(self.inventory[NAVIGATION]['before_sha256'], provenance['sha256'])
        result = subprocess.run(['git', '-c', 'core.autocrlf=false', 'apply', '-'],
                                input=self.sections[NAVIGATION], cwd=self.directory,
                                capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertEqual(self.inventory[NAVIGATION]['after_sha256'], port.digest(source.read_bytes()))
        return source.read_text('utf-8')

    def test_actual_official_navigation_gets_kpm_immediately_before_superuser(self):
        text = self.navigation()
        names = re.findall(r'^    ([A-Za-z]+)\(\n', text, re.M)
        self.assertEqual(['Home', 'Kpm', 'SuperUser', 'Module', 'Settings'], names)
        kpm = text.split('    Kpm(\n', 1)[1].split('    SuperUser(\n', 1)[0]
        self.assertIn('{ bottomPadding -> KpmScreen(bottomPadding) }', kpm)
        self.assertIn('R.string.kpm_tab,', kpm)
        # A real runtime probe lives in the page; the tab remains reachable
        # in preview mode so a missing kernel cannot silently hide the entry.
        self.assertIn('        false,\n', kpm)
        self.assertIn('BottomBarDestination.entries.filter', text)
        self.assertIn('!it.rootRequired', text)

    def test_only_main_navigation_opens_kpm_and_page_respects_the_bottom_bar(self):
        for relative in ('ui/NavContainer.kt', 'ui/navigation/Routes.kt', 'ui/screen/main/SettingsPage.kt'):
            self.assertNotIn(BASE + relative, self.inventory)
        self.assertNotIn(b'Route.Kpm', self.patch)
        screen = added_file(self.sections[BASE + 'ui/screen/kpm/KpmScreen.kt']).decode()
        self.assertIn('fun KpmScreen(bottomPadding: Dp)', screen)
        self.assertNotIn('AppBackButton', screen)
        self.assertNotIn('navigator.pop()', screen)
        self.assertIn('adaptiveScaffoldWindowInsets(includeBottom = false)', screen)
        self.assertIn('Modifier.padding(bottom = bottomPadding + 5.dp)', screen)
        self.assertIn('padding.calculateBottomPadding() + bottomPadding + 88.dp', screen)
        self.assertIn('stringResource(R.string.kpm_unsupported)', screen)
        self.assertIn('if (state.available)', screen)
        self.assertIn('enabled = state.available && !state.busy', screen)

    def test_short_localized_tab_label_and_five_tab_floating_layout(self):
        for language in ('values', 'values-zh-rCN'):
            name = f'manager/app/src/main/res/{language}/strings.xml'
            additions = added_file(self.sections[name]).decode()
            root = ET.fromstring('<resources>' + additions + '</resources>')
            label = root.find("string[@name='kpm_tab']")
            self.assertIsNotNone(label)
            self.assertEqual('KPM', label.text)
            self.assertIsNone(root.find("string[@name='kpm_entry_summary']"))
        layout = self.sections[BASE + 'ui/component/NavigationBar.kt'].decode()
        self.assertIn('if (destinations.size > 4) 56.dp else 76.dp', layout)
        # Keep five minimum-width tabs and the native 8dp inner / 24dp outer
        # padding within a normal 320dp phone viewport.
        self.assertLessEqual(5 * 56 + 8 + 24, 320)


if __name__ == '__main__':
    unittest.main()
