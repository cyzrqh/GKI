"""Use the shared SukiSU-style KPM page with BakaSU's existing authorization."""
import argparse
from pathlib import Path
import re

from additional_manager_kpm import template_files
from additional_manager_kpm_ui import render_kpm_templates
from configure_bakasu_manager_notice import read_source

NAMESPACE = 'org.bakasu.bakasu'
JAVA = 'manager/app/src/main/java/' + NAMESPACE.replace('.', '/')
SCREEN = JAVA + '/ui/screen/kpm/KpmScreen.kt'
WRAPPER = '''package org.bakasu.bakasu.ui.screen.kpm

import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import org.bakasu.bakasu.ui.kpm.GkiKpmScreen

@Composable
fun KpmScreen(bottomPadding: Dp) {
    GkiKpmScreen(bottomPadding = maxOf(104.dp, bottomPadding + 16.dp))
}
'''


def configure(source: Path):
    source = source.resolve(strict=True)
    _, _, kbuild, _ = read_source(source, 'kernel/Kbuild')
    if not re.search(r'(?m)^REPO_NAME\s*:=\s*(?:BakaSU|ReSukiSU)\s*$', kbuild):
        raise ValueError('Expected native BakaSU source')
    original = template_files()[SCREEN]
    path, _, current, _ = read_source(source, SCREEN)
    if current not in (original, WRAPPER):
        raise ValueError('Review changed BakaSU KPM page before replacing its presentation')
    # BakaSU's native MaterialExpressiveTheme already owns its dynamic/dark palette.
    generated = render_kpm_templates(NAMESPACE, 'bakasu', theme_api='material3')
    plan = {path: WRAPPER, **{source / JAVA / 'ui/kpm' / name: text for name, text in generated.items()}}
    for target in plan:
        if any(p.is_symlink() for p in (target, *target.parents) if p != source):
            raise ValueError('Refusing a linked BakaSU UI output')
    for target, text in plan.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8', newline='\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    configure(parser.parse_args().source)
