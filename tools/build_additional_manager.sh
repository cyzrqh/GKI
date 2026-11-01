#!/usr/bin/env bash
# Build a paired ARM64 manager from its own fixed source; final signing is separate.
set -euo pipefail
build_tools="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

source_dir=""
output_dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --source|--output)
      [ "$#" -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
      if [ "$1" = --source ]; then source_dir="$2"; else output_dir="$2"; fi
      shift 2
      ;;
    -h|--help)
      echo 'Usage: build_additional_manager.sh --source ROOT --output DIR'
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$source_dir" ] && [ -n "$output_dir" ] || {
  echo 'Both --source and --output are required.' >&2
  exit 2
}
source_dir="$(cd "$source_dir" && pwd -P)"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd -P)"
manager_dir="$source_dir/manager"
python3 "$build_tools/manager_ui_defaults.py" --source "$source_dir" --variant "$KSU_VARIANT"
if [ "$KSU_VARIANT" = FinaSU ]; then
  exec python3 "$build_tools/build_fina_manager.py" --source "$source_dir" --output "$output_dir"
fi
for required in Cargo.toml Cargo.lock userspace/ksud/Cargo.toml .github/scripts/setup-rust-build.sh manager/gradlew manager/app/build.gradle.kts; do
  [ -f "$source_dir/$required" ] || { echo "Missing native manager build source: $required" >&2; exit 1; }
done
: "${ANDROID_HOME:?ANDROID_HOME must point to the prepared Android SDK}"
ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
: "${ANDROID_NDK_HOME:?ANDROID_NDK_HOME must point to the prepared Android NDK}"
export ANDROID_NDK_HOME
export CARGO_TARGET_DIR="$source_dir/target"
expected_package=$(python3 - "$build_tools" "$KSU_VARIANT" <<'PYCODE'
import sys
sys.path.insert(0,sys.argv[1])
from manager_variants import manager_profile
print(manager_profile(sys.argv[2])['package'])
PYCODE
)
[ "${KSU_PACKAGE_NAME:-}" = "$expected_package" ] || { echo 'Manager package differs from its official application ID.' >&2; exit 1; }
export KSU_PACKAGE_NAME
[ -d "$ANDROID_HOME" ] && [ -d "$ANDROID_NDK_HOME" ] || { echo 'Android SDK or NDK directory does not exist.' >&2; exit 1; }

# The caller stages and validates the same-source, same-certificate modules.
# Refuse an incomplete asset directory before running either native compiler.
python3 - "$source_dir" "$build_tools" <<'PY'
from pathlib import Path
import struct
import sys
sys.path.insert(0, sys.argv[2])
from manager_lkm_jobs import KMIS
assets = Path(sys.argv[1]) / 'userspace/ksud/bin/aarch64'
expected = {kmi + '_kernelsu.ko' for kmi in KMIS}
actual = {path.name for path in assets.glob('*_kernelsu.ko')}
if actual != expected:
    raise SystemExit('The manager requires all eight verified native KMI modules; missing: '
                     + ', '.join(sorted(expected - actual)) + '; unexpected: '
                     + ', '.join(sorted(actual - expected)))
for name in sorted(expected):
    path = assets / name
    if path.is_symlink() or not path.is_file() or not 64 <= path.stat().st_size <= 64 * 1024 * 1024:
        raise SystemExit('Missing or invalid staged manager module: ' + name)
    with path.open('rb') as stream:
        header = stream.read(64)
    if (header[:7] != b'\x7fELF\x02\x01\x01'
            or struct.unpack_from('<HHI', header, 16) != (1, 183, 1)):
        raise SystemExit('The staged manager module is not an ARM64 ELF object: ' + name)
print('Manager native KMI inputs: ' + ', '.join(KMIS))
PY

cd "$source_dir"
echo "Paired manager source: $(git rev-parse HEAD)"
echo "Upstream source version: $(git describe --tags --always)"
java -version
rustc --version
cargo --version

python3 "$build_tools/prepare_additional_gradle.py" --source "$source_dir" --variant "$KSU_VARIANT"

# The upstream helper configures the Android linker, archiver and bindgen sysroot.
source .github/scripts/setup-rust-build.sh aarch64-linux-android 26
if [ -f userspace/ksuinit/Cargo.toml ]; then
  builtins="$("$CLANG_PATH" --print-resource-dir)/lib/linux/libclang_rt.builtins-aarch64-android.a"
  [ -f "$builtins" ] || { echo 'NDK ARM64 compiler builtins were not found.' >&2; exit 1; }
  echo 'Building ARM64 ksuinit from the same source.'
  RUSTFLAGS="-C target-feature=+crt-static -C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wno-unused-command-line-argument -C link-arg=$builtins" \
    cargo build --locked --manifest-path userspace/ksuinit/Cargo.toml --target aarch64-linux-android --release
  test -s "$CARGO_TARGET_DIR/aarch64-linux-android/release/ksuinit"
  mkdir -p userspace/ksud/bin/aarch64
  cp "$CARGO_TARGET_DIR/aarch64-linux-android/release/ksuinit" userspace/ksud/bin/aarch64/ksuinit
fi

echo 'Building ARM64 ksud from the same source.'
# Upstream enables rust-embed compression and explicitly requires cleaning
# this crate after its binary assets change. Retain compiled dependencies.
cargo clean -p ksud --target aarch64-linux-android --release
cargo build --locked --target aarch64-linux-android --release --manifest-path userspace/ksud/Cargo.toml
ksud="$CARGO_TARGET_DIR/aarch64-linux-android/release/ksud"
[ -s "$ksud" ] || { echo 'The ARM64 ksud build produced no daemon.' >&2; exit 1; }
python3 - "$ksud" "$build_tools" <<'PY'
from pathlib import Path
import struct
import sys
sys.path.insert(0, sys.argv[2])
from manager_lkm_jobs import KMIS
data = Path(sys.argv[1]).read_bytes()
if (len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01'
        or struct.unpack_from('<H', data, 16)[0] not in (2, 3)
        or struct.unpack_from('<H', data, 18)[0] != 183):
    raise SystemExit('The built ksud is not an ARM64 ELF executable.')
missing = [kmi for kmi in KMIS if (kmi + '_kernelsu.ko').encode('ascii') not in data]
if missing:
    raise SystemExit('The built ksud is missing embedded native KMI modules: ' + ', '.join(missing))
print('Verified eight native KMI assets in the built ARM64 ksud.')
PY
mkdir -p "$manager_dir/app/src/main/jniLibs/arm64-v8a"
cp "$ksud" "$manager_dir/app/src/main/jniLibs/arm64-v8a/libksud.so"

cd "$manager_dir"
echo 'Building unsigned ARM64 release APK; signing is handled by the caller.'
bash ./gradlew --no-daemon --stacktrace --build-cache assembleRelease -PKSU_PACKAGE_NAME="$KSU_PACKAGE_NAME"

python3 - "$manager_dir/app/build/outputs/apk/release" "$output_dir" "$build_tools" <<'PY'
from pathlib import Path
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
sys.path.insert(0, sys.argv[3])
from manager_lkm_jobs import KMIS
release, output = map(Path, sys.argv[1:3])
apks = sorted(release.glob('*.apk'))
if len(apks) != 1:
    raise SystemExit(f'Expected exactly one release APK, found {len(apks)}.')
apk = apks[0]
# Next and official managers assign an output filename ending in -release.apk
# even when Gradle signing is disabled. Do not infer signing state from a name.
if (not re.fullmatch(r'[A-Za-z0-9_.+@-]+-release(?:-unsigned)?\.apk', apk.name)
        or len(apk.name) > 240):
    raise SystemExit('Expected a bounded native release APK filename.')
with zipfile.ZipFile(apk) as archive:
    required = {'AndroidManifest.xml', 'classes.dex', 'lib/arm64-v8a/libksud.so', 'lib/arm64-v8a/libkernelsu.so'}
    missing = required.difference(archive.namelist())
    if missing:
        raise SystemExit('Incomplete manager APK: ' + ', '.join(sorted(missing)))
    other_abis = {name.split('/')[1] for name in archive.namelist() if name.startswith('lib/') and len(name.split('/')) >= 3}
    if other_abis != {'arm64-v8a'}:
        raise SystemExit('The manager APK unexpectedly contains another ABI.')
    if archive.getinfo('lib/arm64-v8a/libksud.so').file_size == 0:
        raise SystemExit('The packaged ksud is empty.')
    packaged_ksud = archive.read('lib/arm64-v8a/libksud.so')
    header = packaged_ksud[:20]
    if len(header) != 20 or header[:5] != b'\x7fELF\x02' or header[5] != 1 or struct.unpack_from('<H', header, 18)[0] != 183:
        raise SystemExit('The packaged ksud is not a little-endian ARM64 ELF binary.')
    missing_kmis = [kmi for kmi in KMIS if (kmi + '_kernelsu.ko').encode('ascii') not in packaged_ksud]
    if missing_kmis:
        raise SystemExit('The packaged ksud is missing embedded native KMI modules: ' + ', '.join(missing_kmis))
    if archive.testzip() is not None:
        raise SystemExit('The APK ZIP integrity check failed.')
sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools/37.0.0'
aapt2 = next((path for path in (sdk / 'aapt2', sdk / 'aapt2.exe') if path.is_file()), None)
if aapt2 is None:
    raise SystemExit('Android build-tools 37.0.0 aapt2 was not prepared.')
report = subprocess.run([str(aapt2), 'dump', 'badging', str(apk)], capture_output=True,
                        text=True, encoding='utf-8', errors='replace', timeout=180)
if report.returncode:
    raise SystemExit('aapt2 could not read the built manager APK metadata.')
rows = re.findall(r'^package:[^\r\n]*', report.stdout, re.M)
if len(rows) != 1:
    raise SystemExit('Expected one package metadata row in the built manager APK.')
fields = {}
for name in ('name', 'versionName', 'versionCode'):
    values = re.findall(r'(?:^|\s)' + name + r"='([^'\r\n]*)'(?=\s|$)", rows[0])
    if len(values) != 1:
        raise SystemExit('Expected one built manager APK ' + name + '.')
    fields[name] = values[0]
if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', fields['name']):
    raise SystemExit('The built manager application ID is not a valid ASCII package.')
from manager_variants import manager_profile
if fields['name'] != manager_profile(os.environ['KSU_VARIANT'])['package']:
    raise SystemExit('The built manager must retain its own official application ID.')
version = fields['versionName']
if not version or len(version) > 128 or any(ord(char) < 32 or ord(char) > 126 for char in version):
    raise SystemExit('The built manager versionName must be nonempty printable ASCII.')
code = fields['versionCode']
if not re.fullmatch(r'[1-9][0-9]{0,9}', code) or int(code) > 2147483647:
    raise SystemExit('The built manager versionCode is invalid.')
metadata = {'source_apk_filename': apk.name,
            'apk_filename': apk.name.removesuffix('-unsigned.apk') + '.apk' if apk.name.endswith('-unsigned.apk') else apk.name,
            'manager_package': fields['name'], 'manager_version_name': version,
            'manager_version_code': int(code)}
target = output / 'manager-unsigned.apk'
shutil.copyfile(apk, target)
(output / 'manager-build-metadata.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
if os.environ.get('GITHUB_OUTPUT'):
    with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8', newline='\n') as stream:
        for name, value in metadata.items():
            stream.write(f'{name}={value}\n')
print(f'Unsigned ARM64 manager APK: {target} ({target.stat().st_size} bytes)')
print(json.dumps(metadata, sort_keys=True))
PY
