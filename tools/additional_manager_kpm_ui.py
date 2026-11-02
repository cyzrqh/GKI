"""Generate an actual native KPM page and its existing navigation entry."""
from pathlib import Path
import re
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[1]


def render_kpm_templates(namespace: str, shell_api: str) -> dict[str, str]:
    from additional_manager_kpm import replace_one
    template = (ROOT / 'tools/templates/GkiKpmScreen.kt').read_text(encoding='utf-8')
    if shell_api == 'new':
        template = replace_one(template, 'ui.util.getRootShell', 'ui.util.createRootShell')
        anchor = '        val shell = getRootShell()\n'
        if template.count(anchor) != 1:
            raise ValueError('Review the bounded KPM command shell lifetime')
        start = template.index(anchor) + len(anchor)
        end = template.index('\n    }\n\n    suspend fun jsonCommand', start)
        template = (template[:start].replace(anchor, '        val shell = createRootShell()\n')
                    + '        try {\n' + textwrap.indent(template[start:end], '    ')
                    + '\n        } finally {\n            shell.close()\n        }' + template[end:])
    elif shell_api == 'bakasu':
        template = replace_one(template, 'ui.util.getRootShell', 'data.shell.KsuCliRepository')
        template = replace_one(template, '    val context = LocalContext.current\n',
            '    val context = LocalContext.current\n    val cli = org.koin.compose.koinInject<KsuCliRepository>()\n')
        template = replace_one(template, '        val shell = getRootShell()\n',
            '        check(__NAMESPACE__.Natives.isManager && __NAMESPACE__.Natives.isFullFeatured()) { "需要配套管理器授权" }\n'
            '        val shell = cli.getRootShell()\n')
    elif shell_api != 'cached':
        raise ValueError('Unknown native manager shell ownership')
    documents = {'GkiKpmScreen.kt': template,
                 'GkiKpmModule.kt': (ROOT / 'tools/templates/GkiKpmModule.kt').read_text(encoding='utf-8')}
    result = {}
    for name, text in documents.items():
        text = text.replace('__NAMESPACE__', namespace)
        # Placeholder expansion changes lexicographic import order.
        imports = re.findall(r'^import .*$', text, re.M)
        if imports:
            start, end = text.index(imports[0]), text.index(imports[-1]) + len(imports[-1])
            text = text[:start] + '\n'.join(sorted(imports)) + text[end:]
        result[name] = text
    return result


def manager_ui_plan(source: Path, reference: str, variant: str) -> dict[str, bytes]:
    from additional_manager_kpm import replace_one
    files = subprocess.check_output(['git', '-C', str(source), 'ls-tree', '-r', '--name-only', reference],
                                    text=True).splitlines()
    planned = {}

    def read(name):
        return subprocess.check_output(['git', '-C', str(source), 'show', reference + ':' + name]).decode()

    def put(name, text):
        planned[name] = text.encode()

    main = next(p for p in files if p.endswith('/ui/MainActivity.kt'))
    namespace = re.search(r'^package (\S+)\.ui$', read(main), re.M)[1]
    java = 'manager/app/src/main/java/' + namespace.replace('.', '/')
    cli = read(java + '/ui/util/KsuCli.kt')
    shell_api = 'cached'
    if not re.search(r'\bfun\s+getRootShell\s*\(', cli):
        if not re.search(r'\bfun\s+createRootShell\s*\(', cli):
            raise ValueError('Review the native manager root-shell API before adding KPM')
        shell_api = 'new'
    for name, text in render_kpm_templates(namespace, shell_api).items():
        put(java + '/ui/kpm/' + name, text)
    name = 'manager/app/src/main/res/values/strings.xml'
    put(name, replace_one(read(name), '</resources>', '    <string name="gki_kpm_title">KPM</string>\n</resources>'))

    if variant == 'KernelSU-Next':
        name = java + '/ui/screen/BottomBarDestination.kt'
        text = read(name)
        text = replace_one(text, 'import com.ramcosta.composedestinations.spec.DirectionDestinationSpec',
            'import com.ramcosta.composedestinations.generated.destinations.GkiKpmScreenDestination\n'
            'import com.ramcosta.composedestinations.spec.DirectionDestinationSpec')
        text = replace_one(text, '    SuperUser(',
            '    Kpm(GkiKpmScreenDestination, R.string.gki_kpm_title, Icons.Filled.Layers, Icons.Outlined.Layers, true),\n'
            '    SuperUser(')
        put(name, text)
        # Use the very same generated navigation mechanism as the native tabs.
        wrapper = ('package ' + namespace + '.ui.screen\n\n'
            'import androidx.compose.runtime.Composable\n'
            'import com.ramcosta.composedestinations.annotation.Destination\n'
            'import com.ramcosta.composedestinations.annotation.RootGraph\n\n'
            '@Destination<RootGraph>\n@Composable\nfun GkiKpmScreen() {\n'
            '    ' + namespace + '.ui.kpm.GkiKpmScreen()\n}\n')
        put(java + '/ui/screen/GkiKpmScreen.kt', wrapper)
    else:
        candidates = [p for p in files if p.endswith(('/BottomBarMiuix.kt', '/BottomBar.kt'))
                      and 'enum class BottomBarDestination(' in read(p)]
        if len(candidates) != 1:
            raise ValueError('Review the native manager bottom-bar enum')
        name = candidates[0]
        text = read(name)
        if variant == 'FinaSU':
            entry = '    Kpm(R.string.gki_kpm_title, Icons.Filled.Extension, Icons.Outlined.Extension, "KPM"),\n'
        else:
            entry = '    Kpm(R.string.gki_kpm_title, Icons.Rounded.Security),\n'
        put(name, replace_one(text, '    SuperUser(', entry + '    SuperUser('))
        text = read(main)
        if variant == 'FinaSU':
            text = replace_one(text,
                '    val destinations = remember(navVisibilityRevision) { NavBarVisibilityConfig.visibleDestinations() }',
                '    val kpmAuthorized = LocalCapabilityState.current.privileged\n'
                '    val destinations = remember(navVisibilityRevision, kpmAuthorized) {\n'
                '        NavBarVisibilityConfig.visibleDestinations().filter {\n'
                '            it != BottomBarDestination.Kpm || kpmAuthorized\n'
                '        }\n'
                '    }')
        if 'BottomBarDestination.SuperUser ->' in text:
            text = replace_one(text, '                        BottomBarDestination.SuperUser ->',
                '                        BottomBarDestination.Kpm -> ' + namespace + '.ui.kpm.GkiKpmScreen()\n'
                '                        BottomBarDestination.SuperUser ->')
        else:
            # Native official/Kow pages are indexed. Shift all three old page
            # dispatch arms together, not the destinations' persisted IDs.
            pattern = re.compile(r'(?m)^([ \t]*)([123]) -> ((?:if .*?)?(?:SuperUserPager|ModulePager|SettingPager)\(.*)$')
            matches = list(pattern.finditer(text))
            if len(matches) != 3:
                raise ValueError('Review the native indexed pager content before adding KPM')
            text = pattern.sub(lambda m: m[1] + str(int(m[2]) + 1) + ' -> ' + m[3], text)
            match = re.search(r'(?m)^([ \t]*)2 -> (?:if .*?)?SuperUserPager', text)
            text = text[:match.start()] + match[1] + '1 -> ' + namespace + '.ui.kpm.GkiKpmScreen()\n' + text[match.start():]
            config = java + '/ui/viewmodel/MainActivityViewModel.kt'
            put(config, replace_one(read(config), 'const val PAGE_COUNT = 4', 'const val PAGE_COUNT = 5'))
            # Material bars/rails keep their own ordered icon triples, whereas
            # Miuix derives them from the enum. Keep both styles on the same
            # five destinations so SuperUser/Modules/Settings cannot point at
            # the wrong page after inserting KPM.
            anchor = '        Triple(R.string.superuser, Icons.Filled.Shield, Icons.Outlined.Shield),'
            for name in files:
                if not (name.startswith(java + '/ui/component/bottombar/') and name.endswith('.kt')):
                    continue
                native = read(name)
                if anchor in native:
                    put(name, replace_one(native, anchor,
                        '        Triple(R.string.gki_kpm_title, Icons.Filled.Extension, Icons.Outlined.Extension),\n' + anchor))
        put(main, text)
    return planned
