"""Verify failed build.sh runs keep reusable objects without gating builds on cache I/O."""
import re
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import generate_workflow as generator


def condition_value(expression, *, enabled='true', local='false', started='true',
                    outcome='success', cancelled=False, restored='false', saved='false'):
    values = {
        'steps.ccache-config.outputs.enabled': enabled,
        'env.ABK_LOCAL_CACHE_ENABLED': local,
        'env.CCACHE_COMPILE_STARTED': started,
        'steps.compile-kernel.outcome': outcome,
        'steps.persistent-ccache-restore.outputs.restored': restored,
        'steps.persistent-ccache-save.outputs.saved': saved,
    }
    for name, value in values.items():
        expression = expression.replace(name, repr(value))
    expression = expression.replace('always()', 'True').replace('cancelled()', repr(cancelled))
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    expression = re.sub(r'!(?!=)', ' not ', expression)
    return bool(eval(expression.strip(), {'__builtins__': {}}, {}))


def render_key(template, **overrides):
    values = {'runner.os': 'Linux', 'runner.arch': 'X64', 'inputs.android_version': 'android13',
              'inputs.kernel_version': '5.15', 'inputs.sub_level': '41',
              'inputs.os_patch_level': '2022-06', 'github.run_id': '100', 'github.run_attempt': '1',
              "hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')": 'a' * 64}
    values.update(overrides)
    values.update({name.replace('inputs.', 'matrix.'): value for name, value in list(values.items())
                   if name.startswith('inputs.')})
    return re.sub(r'\$\{\{\s*(.*?)\s*\}\}', lambda match: values[match.group(1)], template)


class KernelCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.steps = generator.create_workflow(ROOT)['jobs']['build-kernel']['steps']

    def find(self, name):
        return next(step for step in self.steps if step.get('name') == name)

    def test_cache_detects_build_sh_after_sync_including_historical_61(self):
        detection = self.find('选择 build.sh 编译缓存')
        self.assertNotIn('if', detection)
        self.assertIn('if [ -f "$KERNEL_ROOT/build/build.sh" ]; then', detection['run'])
        self.assertIn("echo 'enabled=true'", detection['run'])
        self.assertIn("echo 'enabled=false'", detection['run'])
        self.assertNotIn('inputs.kernel_version', detection['run'])
        self.assertLess(self.steps.index(self.find('初始化并同步内核源码')), self.steps.index(detection))
        measurement = self.find('开始记录实际 ccache 命中')
        self.assertEqual(measurement['if'], "steps.ccache-config.outputs.enabled == 'true'")

    def test_restore_and_save_use_same_data_and_best_effort_transfer_limits(self):
        restore, save = self.find('恢复 ccache 缓存'), self.find('保存本次 ccache 编译结果')
        self.assertEqual(restore['uses'], 'actions/cache/restore@v5')
        self.assertEqual(save['uses'], 'actions/cache/save@v5')
        self.assertEqual(restore['with']['key'], save['with']['key'])
        self.assertEqual(restore['with']['path'], '~/.ccache')
        self.assertEqual(restore['with']['path'], save['with']['path'])
        for step in (restore, save):
            self.assertIs(step['continue-on-error'], True)
            self.assertEqual(step['timeout-minutes'], 3)
        self.assertEqual(restore['env']['SEGMENT_DOWNLOAD_TIMEOUT_MINS'], '2')

    def test_each_target_month_run_and_attempt_can_save_fresh_objects(self):
        key = self.find('恢复 ccache 缓存')['with']['key']
        reference = render_key(key)
        for changed in ({'github.run_id': '101'}, {'github.run_attempt': '2'},
                        {'inputs.os_patch_level': '2022-07'}, {'inputs.sub_level': '74'},
                        {'runner.arch': 'ARM64'}):
            with self.subTest(changed=changed):
                self.assertNotEqual(reference, render_key(key, **changed))
        self.assertNotIn('github.sha', key)
        prefixes = self.find('恢复 ccache 缓存')['with']['restore-keys'].splitlines()
        self.assertTrue(reference.startswith(render_key(prefixes[0])))
        self.assertTrue(render_key(key, **{'inputs.os_patch_level': '2022-07'}).startswith(render_key(prefixes[-1])))
        self.assertFalse(render_key(key, **{'inputs.sub_level': '74'}).startswith(render_key(prefixes[-1])))
        # Guarded ECS cannot import stock or unbound public object caches.
        for prefix in prefixes:
            self.assertTrue(prefix.startswith('gki-ccache-ecs-v1-'))
            self.assertIn("hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')", prefix)
        changed = {"hashFiles('config/kernel-cache-tools.json', 'tools/kernel_compile_cache.py')": 'b' * 64}
        self.assertNotEqual(reference, render_key(key, **changed))

    def test_failures_after_compilation_save_but_precompile_failures_do_not(self):
        condition = self.find('保存本次 ccache 编译结果')['if']
        for outcome in ('success', 'failure'):
            self.assertTrue(condition_value(condition, outcome=outcome))
        self.assertFalse(condition_value(condition, saved='true'))
        for parameters in ({'started': 'false'}, {'started': ''}, {'outcome': 'skipped'},
                           {'outcome': 'cancelled'}, {'outcome': ''}, {'enabled': 'false'},
                           {'local': 'true'}, {'cancelled': True}):
            with self.subTest(parameters=parameters):
                self.assertFalse(condition_value(condition, **parameters))

    def test_marker_is_written_only_on_the_actual_build_sh_compilation_path(self):
        command = self.find('编译内核')['with']['command']
        marker = "echo 'CCACHE_COMPILE_STARTED=true' >> \"$GITHUB_ENV\""
        self.assertEqual(command.count(marker), 1)
        build_branch = command.index('if [ -f "build/build.sh" ]; then')
        invocation = command.index('LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 build/build.sh')
        self.assertLess(build_branch, command.index(marker))
        self.assertLess(command.index(marker), invocation)
        self.assertLess(invocation, command.index('\nelse\n', invocation))

    def test_statistics_start_after_restoration_and_saving_follows_compile(self):
        restore = self.find('恢复 ccache 缓存')
        measurement = self.find('开始记录实际 ccache 命中')
        compile_step = self.find('编译内核')
        save = self.find('保存本次 ccache 编译结果')
        self.assertLess(self.steps.index(restore), self.steps.index(measurement))
        self.assertLess(self.steps.index(measurement), self.steps.index(compile_step))
        self.assertLess(self.steps.index(compile_step), self.steps.index(save))
        self.assertIn('ccache --zero-stats', measurement['run'])
        restore_condition = restore['if']
        self.assertTrue(condition_value(restore_condition))
        self.assertFalse(condition_value(restore_condition, restored='true'))
        self.assertFalse(condition_value(restore_condition, enabled='false'))
        self.assertFalse(condition_value(restore_condition, local='true'))


if __name__ == '__main__':
    unittest.main()
