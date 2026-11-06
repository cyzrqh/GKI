"""Bridge KPM's Material components to the manager's active native theme."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def detect_kpm_theme(namespace: str, theme: str, ui_mode: str = '') -> str:
    """Recognize the reviewed providers, never infer dark mode from the device."""
    if 'MiuixKernelSUTheme(' in theme:
        package = re.search(r'^package (\S+)\s*$', ui_mode, re.M)
        if (package is None or package[1] != namespace + '.ui'
                or not re.search(r'\bval\s+LocalUiMode\s*=', ui_mode)
                or not re.search(r'\benum\s+class\s+UiMode\b', ui_mode)
                or not re.search(r'UiMode\.Miuix\s*->\s*MiuixKernelSUTheme\(', theme)
                or not re.search(r'UiMode\.Material\s*->\s*MaterialKernelSUTheme\(', theme)
                or 'LocalUiMode.current' not in theme):
            raise ValueError('Review the native manager Material/Miuix theme selector before adding KPM')
        return 'miuix-switch'
    if re.search(r'\bMiuixTheme\s*\(', theme):
        if re.search(r'\bMaterial(?:Expressive)?Theme\s*\(', theme):
            raise ValueError('Review the native manager theme nesting before adding KPM')
        return 'miuix'
    if re.search(r'\b(?:Material(?:Expressive)?Theme|MaterialKernelSUTheme)\s*\(', theme):
        return 'material3'
    raise ValueError('Review the native manager theme provider before adding KPM')


def render_kpm_theme(theme_api: str) -> str:
    if theme_api == 'material3':
        # Keep the parent's exact scheme, fonts, dark override and dynamic colors.
        return '''package __NAMESPACE__.ui.kpm

import androidx.compose.runtime.Composable

@Composable
internal fun GkiKpmTheme(content: @Composable () -> Unit) {
    content()
}
'''
    if theme_api not in ('miuix', 'miuix-switch'):
        raise ValueError('Unknown native manager KPM theme provider')
    text = (ROOT / 'tools/templates/GkiKpmTheme.kt').read_text(encoding='utf-8')
    imports = ('import __NAMESPACE__.ui.LocalUiMode\nimport __NAMESPACE__.ui.UiMode'
               if theme_api == 'miuix-switch' else '')
    condition = 'LocalUiMode.current == UiMode.Miuix' if theme_api == 'miuix-switch' else 'true'
    return text.replace('__THEME_IMPORTS__', imports).replace('__USE_MIUIX__', condition)
