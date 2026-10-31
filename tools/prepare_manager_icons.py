"""Repair the official trimmed icon JAR when it lacks Kotlin module metadata."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib
import zipfile


LOCAL = re.compile(r'^(?P<indent>[ \t]*)implementation\(files\("libs/material-icons-custom\.jar"\)\)[ \t]*$', re.M)
EXTENDED = re.compile(r'^[ \t]*implementation\(libs\.androidx\.compose\.material\.icons\.extended\)[ \t]*$', re.M)


def prepare_manager_icons(source: Path, *, apply: bool = True) -> bool:
    """Keep valid upstream dependencies; replace only the broken local icon input.

    Kotlin top-level icon extension properties require the JAR's kotlin_module
    index, even when their *Kt.class files are present. Use the source's own
    Google Maven alias/BOM rather than guessing versions or merging binary JARs.
    R8 still removes unused icons from the release APK.
    """
    manager = Path(source) / 'manager'
    gradle = manager / 'app/build.gradle.kts'
    if gradle.is_symlink() or not gradle.is_file() or gradle.stat().st_size > 1024 * 1024:
        raise ValueError('Manager Gradle source is missing or invalid')
    text = gradle.read_text('utf-8')
    matches = list(LOCAL.finditer(text))
    if not matches:
        return False
    if len(matches) != 1:
        raise ValueError('Ambiguous custom material icons dependency; source review required')
    jar = manager / 'app/libs/material-icons-custom.jar'
    if jar.is_symlink() or not jar.is_file() or not 0 < jar.stat().st_size <= 64 * 1024 * 1024:
        raise ValueError('The official custom material icons JAR is missing or invalid')
    try:
        with zipfile.ZipFile(jar) as archive:
            entries = archive.infolist()
            if len(entries) > 100000:
                raise ValueError('Custom material icons JAR has too many entries')
            icons = any(info.filename.startswith('androidx/compose/material/icons/')
                        and info.filename.endswith('Kt.class') for info in entries)
            metadata = any(info.filename.startswith('META-INF/')
                           and info.filename.endswith('.kotlin_module') and info.file_size > 0
                           for info in entries)
    except zipfile.BadZipFile:
        raise ValueError('The official custom material icons JAR is corrupt') from None
    if not icons:
        raise ValueError('The official custom material icons JAR contains no icon classes')
    if metadata:
        return False
    catalog = manager / 'gradle/libs.versions.toml'
    if catalog.is_symlink() or not catalog.is_file() or catalog.stat().st_size > 1024 * 1024:
        raise ValueError('Cannot restore icons without the official dependency catalog')
    dependency = tomllib.loads(catalog.read_text('utf-8')).get('libraries', {}).get(
        'androidx-compose-material-icons-extended')
    if not isinstance(dependency, dict) or not (
        dependency.get('group') == 'androidx.compose.material'
        and dependency.get('name') == 'material-icons-extended'
        or dependency.get('module') == 'androidx.compose.material:material-icons-extended'
    ):
        raise ValueError('The source does not declare the official extended material icons dependency')
    match = matches[0]
    replacement = '' if EXTENDED.search(text) else (
        match['indent'] + 'implementation(libs.androidx.compose.material.icons.extended)')
    repaired = text[:match.start()] + replacement + text[match.end():]
    if apply:
        gradle.write_text(repaired, encoding='utf-8', newline='\n')
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        restored = prepare_manager_icons(args.source)
    except (OSError, ValueError) as error:
        print('::error::Manager icon dependency preparation failed: ' + str(error), file=sys.stderr)
        return 1
    print('Manager icons: restored the official full dependency because the trimmed JAR lacks Kotlin module metadata.'
          if restored else 'Manager icons: original dependency configuration retained.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
