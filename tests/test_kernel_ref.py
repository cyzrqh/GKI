from pathlib import Path
import subprocess
import sys
import shutil
import unittest
import uuid
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from set_kernel_ref import resolve_kernel_ref, set_kernel_ref, validate_ref


BRANCH = "android14-6.1-2025-09"
REF = f"refs/heads/{BRANCH}"


class KernelRefTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f".kernel-ref-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_only_common_revision_changes(self):
        default = self.write("default.xml", '<manifest><!-- keep --><remote name="aosp" fetch=".."/><default revision="old" remote="aosp"/><project name="kernel/common" path="common" revision="old" groups="x"/><project name="toolchain" path="tools" revision="tools-ref"/></manifest>')
        ref, count = set_kernel_ref(self.root, REF, BRANCH)
        self.assertEqual((ref, count), (REF, 1))
        tree = ET.parse(default).getroot()
        self.assertEqual(tree.find("default").attrib, {"revision": "old", "remote": "aosp"})
        common, other = tree.findall("project")
        self.assertEqual(common.attrib, {"name": "kernel/common", "path": "common", "revision": REF, "groups": "x"})
        self.assertEqual(other.get("revision"), "tools-ref")
        self.assertIn("<!-- keep -->", default.read_text(encoding="utf-8"))

    def test_reachable_includes_only(self):
        self.write("default.xml", '<manifest><include name="parts/kernel.xml"/></manifest>')
        included = self.write("parts/kernel.xml", '<manifest><project name="common" path="common" revision="old"/></manifest>')
        unused = self.write("unused.xml", '<manifest><project path="common" revision="untouched"/></manifest>')
        self.assertEqual(set_kernel_ref(self.root, REF, BRANCH), (REF, 1))
        self.assertEqual(ET.parse(included).find("project").get("revision"), REF)
        self.assertEqual(ET.parse(unused).find("project").get("revision"), "untouched")

    def test_include_traversal_rejected_before_mutation(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/><include name="../escape.xml"/></manifest>')
        original = default.read_bytes()
        with self.assertRaises(ValueError):
            set_kernel_ref(self.root, REF, BRANCH)
        self.assertEqual(default.read_bytes(), original)

    def test_cycle_rejected_before_mutation(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/><include name="parts.xml"/></manifest>')
        self.write("parts.xml", '<manifest><include name="default.xml"/></manifest>')
        original = default.read_bytes()
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            set_kernel_ref(self.root, REF, BRANCH)
        self.assertEqual(default.read_bytes(), original)

    def test_missing_common_does_not_resolve_or_mutate(self):
        default = self.write("default.xml", '<manifest><project path="tools" revision="old"/></manifest>')
        original = default.read_bytes()
        resolver = unittest.mock.Mock()
        with self.assertRaisesRegex(ValueError, "No common"):
            set_kernel_ref(self.root, "", BRANCH, resolver)
        resolver.assert_not_called()
        self.assertEqual(default.read_bytes(), original)

    def test_invalid_ref_is_rejected(self):
        for ref in ["main", "refs/heads/a;echo", "refs/heads/a..b", "refs/heads/a//b", "refs/tags/.hidden", "refs/heads/a.lock", "refs/heads/a/", "refs/heads/a\\b"]:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                validate_ref(ref)

    def test_empty_ref_uses_resolver(self):
        self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        resolver = unittest.mock.Mock(return_value=f"refs/heads/deprecated/{BRANCH}")
        self.assertEqual(set_kernel_ref(self.root, "", BRANCH, resolver), (f"refs/heads/deprecated/{BRANCH}", 1))
        resolver.assert_called_once_with(BRANCH)

    def test_resolver_failure_does_not_mutate(self):
        default = self.write("default.xml", '<manifest><project path="common" revision="old"/></manifest>')
        original = default.read_bytes()
        with self.assertRaises(ValueError):
            set_kernel_ref(self.root, "", BRANCH, lambda _: "invalid")
        self.assertEqual(default.read_bytes(), original)

    def remote(self, refs):
        return subprocess.CompletedProcess([], 0, "\n".join("a" * 40 + "\t" + ref for ref in refs), "")

    @patch("set_kernel_ref.subprocess.run")
    def test_live_preferred_to_deprecated_and_tags(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r99", f"refs/heads/deprecated/{BRANCH}", REF])
        self.assertEqual(resolve_kernel_ref(BRANCH), REF)
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn("shell", run.call_args.kwargs)

    @patch("set_kernel_ref.subprocess.run")
    def test_deprecated_preferred_to_tags(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r99", f"refs/heads/deprecated/{BRANCH}"])
        self.assertEqual(resolve_kernel_ref(BRANCH), f"refs/heads/deprecated/{BRANCH}")

    @patch("set_kernel_ref.subprocess.run")
    def test_latest_tag_compares_revision_numerically(self, run):
        run.return_value = self.remote([f"refs/tags/{BRANCH}_r2", f"refs/tags/{BRANCH}_r10", f"refs/tags/{BRANCH}_r9", f"refs/tags/{BRANCH}_r100^{{}}"])
        self.assertEqual(resolve_kernel_ref(BRANCH), f"refs/tags/{BRANCH}_r10")

    @patch("set_kernel_ref.subprocess.run")
    def test_missing_remote_refs_fails(self, run):
        run.return_value = self.remote([])
        with self.assertRaisesRegex(ValueError, "No live"):
            resolve_kernel_ref(BRANCH)

    @patch("set_kernel_ref.subprocess.run")
    def test_bad_branch_never_runs_git(self, run):
        for branch in ["-bad", "android14-6.1-2025-13", "android14-6.1-2025-09;echo"]:
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                resolve_kernel_ref(branch)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
