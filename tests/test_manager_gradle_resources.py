from pathlib import Path
import tempfile
import unittest

from manager_gradle_resources import configure


class ManagerGradleResourcesTests(unittest.TestCase):
    def test_bounded_memory_preserves_other_upstream_flags_for_both_gradle_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for project in ('manager', 'userspace/sui'):
                path = root / project / 'gradle.properties'
                path.parent.mkdir(parents=True)
                path.write_text('org.gradle.jvmargs=-Xmx2048m -XX:MaxMetaspaceSize=1g -Dkotlin.daemon.jvm.options\\="-Xmx2028M"\nandroid.useAndroidX=true\n')
            configure(root, 16 * 1024 ** 3, 4)
            for project in ('manager', 'userspace/sui'):
                path = root / project / 'gradle.properties'
                text = path.read_text()
                self.assertIn('-Xmx6144m -XX:MaxMetaspaceSize=1g', text)
                self.assertIn('-Dkotlin.daemon.jvm.options\\="-Xmx2028M"', text)
                self.assertIn('android.useAndroidX=true', text)
                self.assertIn('org.gradle.workers.max=4', text)
                configure(root, 16 * 1024 ** 3, 4)
                self.assertEqual(text, path.read_text())

    def test_smaller_runner_is_not_given_six_gibibytes_or_too_many_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'manager').mkdir()
            path = root / 'manager/gradle.properties'
            path.write_text('org.gradle.jvmargs=-Xmx2048m\n')
            configure(root, 2 * 1024 ** 3, 8)
            self.assertIn('-Xmx1024m', path.read_text())
            self.assertIn('org.gradle.workers.max=2', path.read_text())
