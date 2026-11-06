"""Check native authorization wiring independently of KPM payload availability."""
from pathlib import Path
import unittest
from unittest.mock import patch

from additional_manager_kpm_ui import manager_ui_plan, render_kpm_templates
from kpm_authorization import render_kpm_authorization


class KpmAuthorizationTests(unittest.TestCase):
    def test_unrecognized_manager_cannot_create_the_screen_work_scope_or_start_a_probe(self):
        for shell in ('cached', 'new', 'bakasu'):
            with self.subTest(shell=shell):
                screen = render_kpm_templates('example.manager', shell)['GkiKpmScreen.kt']
                wrapper, content = screen.split('private fun GkiKpmScreenContent(', 1)
                wrapper = wrapper.split('fun GkiKpmScreen(', 1)[1]
                self.assertIn('example.manager.Natives.isManager && example.manager.Natives.isFullFeatured()', wrapper)
                self.assertLess(wrapper.index('if (!authorized) return'), wrapper.index('GkiKpmTheme {'))
                for work in ('rememberCoroutineScope()', 'rememberLauncherForActivityResult(',
                             'operation { refresh() }', 'koinInject<KsuCliRepository>()'):
                    self.assertNotIn(work, wrapper)
                self.assertIn('rememberCoroutineScope()', content)
                self.assertIn('JSONObject(jsonCommand("supported", "--json"))', content)

    def test_each_command_rechecks_manager_before_its_real_root_shell_and_daemon(self):
        for api, factory in (('cached', 'getRootShell()'), ('new', 'createRootShell()'),
                             ('bakasu', 'cli.getRootShell()')):
            with self.subTest(api=api):
                screen = render_kpm_templates('example.manager', api)['GkiKpmScreen.kt']
                command = screen.split('suspend fun command(', 1)[1].split('suspend fun jsonCommand(', 1)[0]
                self.assertLess(command.index('Natives.isManager &&'), command.index(factory))
                self.assertLess(command.index('if (!shell.isRoot) throw KpmRootUnavailable()'),
                                command.index('shell.newJob()'))
                self.assertIn('kpmRootOrUnavailable { ' + factory + ' }', command)
                # Only authorization/root acquisition is quiet. Daemon timeout,
                # failure status and bounded output checks are outside the helper.
                self.assertEqual(2, command.count('kpmRootOrUnavailable {'))
                self.assertIn('check(result.isSuccess)', command)
                self.assertIn('result.code == 124', command)
                self.assertIn('it.toByteArray().size <= 16384', command)
                self.assertEqual(api == 'new', 'shell.close()' in command)

    def test_only_the_private_unavailable_signal_is_silent_and_cancellation_survives(self):
        screen = render_kpm_templates('example.manager', 'cached')['GkiKpmScreen.kt']
        operation = screen.split('fun operation(', 1)[1].split('val picker =', 1)[0]
        cancelled = operation.index('catch (cancelled: CancellationException)')
        unavailable = operation.index('catch (_: KpmRootUnavailable)')
        other = operation.index('catch (error: Exception)')
        self.assertLess(cancelled, unavailable)
        self.assertLess(unavailable, other)
        self.assertIn('throw cancelled', operation[cancelled:unavailable])
        quiet = operation[unavailable:other]
        self.assertIn('loaded = emptyList()', quiet)
        self.assertIn('saved = emptyList()', quiet)
        self.assertIn('detail = null', quiet)
        self.assertNotIn('detail = status', quiet)
        self.assertIn('error is org.json.JSONException', operation[other:])
        self.assertIn('detail = status', operation[other:])

    def test_fina_uses_native_capability_snapshot_only_in_composition_and_fresh_gate_for_io(self):
        java = 'manager/app/src/main/java/me/weishu/kernelsu'
        files = {
            java + '/ui/MainActivity.kt': 'package me.weishu.kernelsu.ui\n'
                '    val destinations = remember(navVisibilityRevision) { NavBarVisibilityConfig.visibleDestinations() }\n'
                '                        BottomBarDestination.SuperUser -> SuperUserPager()\n',
            java + '/ui/util/KsuCli.kt': 'fun getRootShell() = nativeShell()\n',
            java + '/ui/theme/Theme.kt': 'fun KernelSUTheme() { MaterialTheme(content = content) }\n',
            java + '/ui/component/bottombar/BottomBar.kt': 'package me.weishu.kernelsu.ui.component.bottombar\n'
                'enum class BottomBarDestination(\n) {\n    SuperUser(original),\n}\n',
            'manager/app/src/main/res/values/strings.xml': '<resources></resources>',
        }

        def git_output(args, **kwargs):
            if 'ls-tree' in args:
                return '\n'.join(files)
            return files[args[-1].split(':', 1)[1]].encode()

        with patch('additional_manager_kpm_ui.subprocess.check_output', side_effect=git_output):
            plan = manager_ui_plan(Path('native-fina'), 'a' * 40, 'FinaSU')
        screen = plan[java + '/ui/kpm/GkiKpmScreen.kt'].decode()
        wrapper = screen.split('fun GkiKpmScreen(', 1)[1].split('private fun GkiKpmScreenContent(', 1)[0]
        self.assertIn('val authorized = me.weishu.kernelsu.ui.util.LocalCapabilityState.current.privileged', wrapper)
        self.assertNotIn('try {', wrapper)  # Compose reads cannot be placed inside try/catch.
        command = screen.split('suspend fun command(', 1)[1].split('suspend fun jsonCommand(', 1)[0]
        self.assertIn('me.weishu.kernelsu.ui.util.CapabilityRepository.currentPrivileged()', command)
        self.assertNotIn('LocalCapabilityState', command)
        self.assertNotIn('Natives.isFullFeatured()', screen)  # Fina removed that API.
        self.assertNotIn('fullLayout', screen)

    def test_incomplete_or_non_composable_command_wiring_requires_review(self):
        template = '__KPM_AUTHORIZATION__\n__KPM_COMMAND_AUTHORIZATION__'
        with self.assertRaisesRegex(ValueError, 'both UI and command gates'):
            render_kpm_authorization(template, 'example.manager', 'uiAuthorized')
        with self.assertRaisesRegex(ValueError, 'Compose authorization snapshot'):
            render_kpm_authorization(template, 'example.manager', 'LocalCapabilityState.current.privileged',
                                     'example.manager.ui.util.LocalCapabilityState.current.privileged')
        with self.assertRaisesRegex(ValueError, 'integration point'):
            render_kpm_authorization('__KPM_AUTHORIZATION__', 'example.manager')


if __name__ == '__main__':
    unittest.main()
