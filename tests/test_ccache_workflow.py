from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import generate_workflow as generator


class CcacheWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.job = generator.create_workflow(ROOT)["jobs"]["build-kernel"]
        cls.steps = cls.job["steps"]

    def step(self, name):
        return next(step for step in self.steps if step.get("name") == name)

    def test_compile_and_configuration_share_the_restored_cache_directory(self):
        run = self.step("配置 ccache")["run"]
        configured = run.index('export CCACHE_DIR="$HOME/.ccache"')
        self.assertLess(configured, run.index("ccache --max-size"))
        self.assertLess(configured, run.index("ccache --set-config"))
        self.assertIn('echo "CCACHE_DIR=$CCACHE_DIR" >> "$GITHUB_ENV"', run)
        self.assertEqual(self.step("恢复 ccache 缓存")["with"]["path"], "~/.ccache")
        self.assertIn('build/build.sh CC="/usr/bin/ccache clang"', self.step("编译内核")["with"]["command"])

    def test_restored_config_cannot_disable_compression_or_change_the_size_limit(self):
        env = self.job["env"]
        # Environment settings take priority even when an old archive restores
        # a ccache.conf created before the current settings were installed.
        self.assertNotIn("CCACHE_HARDLINK", env)
        self.assertEqual(env["CCACHE_NOHARDLINK"], "true")
        self.assertEqual(env["CCACHE_COMPRESS"], "true")
        self.assertEqual(env["CCACHE_MAXSIZE"], "2G")
        self.assertIn("ccache --set-config=hard_link=false", self.step("配置 ccache")["run"])

    def test_compiler_identity_uses_binary_content_without_correctness_relaxations(self):
        env = self.job["env"]
        self.assertEqual(env["CCACHE_COMPILERCHECK"], "content")
        self.assertEqual(env["CCACHE_BASEDIR"], "${{ github.workspace }}")
        self.assertNotIn("CCACHE_SLOPPINESS", env)
        self.assertNotIn("time_macros", str(self.steps))

    def test_statistics_measure_this_compile_including_failure_and_are_not_bazel_metrics(self):
        names = [step.get("name") for step in self.steps]
        reset = self.step("开始记录实际 ccache 命中")
        stats = self.step("显示本次 ccache 命中统计")
        self.assertLess(names.index("恢复 ccache 缓存"), names.index(reset["name"]))
        self.assertLess(names.index(reset["name"]), names.index("编译内核"))
        self.assertLess(names.index("编译内核"), names.index(stats["name"]))
        self.assertEqual(reset["if"], "steps.ccache-config.outputs.enabled == 'true'")
        self.assertIn('if [ -f "$KERNEL_ROOT/build/build.sh" ]', reset["run"])
        self.assertIn("ccache --zero-stats", reset["run"])
        self.assertIn("always()", stats["if"])
        self.assertIn("steps.ccache-measurement.outputs.enabled == 'true'", stats["if"])
        self.assertNotIn("success", stats["if"])
        self.assertIn("ccache --show-stats --verbose", stats["run"])
        self.assertIn('"$GITHUB_STEP_SUMMARY"', stats["run"])
        self.assertTrue(stats["continue-on-error"])
        self.assertNotIn("ccache", self.step("恢复 Bazel 磁盘缓存")["with"]["path"])


if __name__ == "__main__":
    unittest.main()
