#!/usr/bin/env bash
# Build the pinned native assets shipped with the official Nova manager.
set -euo pipefail
source_dir="$1"
build_tools="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
submodule_path=external/BuiltZygisk
submodule_dir="$source_dir/$submodule_path"
dependency_identity="$(python3 "$build_tools/sukisu_builtzygisk_source.py" "$source_dir")"
read -r source_url revision <<< "$dependency_identity"

export GIT_TERMINAL_PROMPT=0
git_https=(git -c protocol.allow=never -c protocol.https.allow=always)
timeout 300 "${git_https[@]}" -c "submodule.$submodule_path.url=$source_url" \
  -C "$source_dir" submodule update --init -- "$submodule_path"
if [ "$(git -C "$submodule_dir" rev-parse HEAD)" != "$revision" ]; then
  echo 'The BuiltZygisk checkout differs from its Nova source pin.' >&2
  exit 1
fi
timeout 300 "${git_https[@]}" -C "$submodule_dir" submodule update --init --recursive
dependencies="$(git -C "$submodule_dir" submodule status --recursive)"
if grep -qE '^[-+U]' <<< "$dependencies"; then
  echo 'The BuiltZygisk dependencies did not match their Git pins.' >&2
  exit 1
fi
echo "Building official Nova BuiltZygisk source $revision"

ndk="$ANDROID_HOME/ndk/27.2.12479018"
if [ ! -f "$ndk/source.properties" ]; then
  timeout 300 "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" 'ndk;27.2.12479018'
fi
toolchain=nightly-2026-09-23
timeout 300 rustup toolchain install "$toolchain" --profile minimal --component rust-src
timeout 180 rustup target add --toolchain "$toolchain" \
  aarch64-linux-android armv7-linux-androideabi x86_64-linux-android
tools_dir=$(mktemp -d)
trap 'rm -rf -- "$tools_dir"' EXIT
timeout 300 cargo +stable install --locked --version '=4.1.2' --root "$tools_dir" cargo-ndk
(
  cd "$submodule_dir"
  export PATH="$tools_dir/bin:$PATH" RUSTUP_TOOLCHAIN="$toolchain"
  export ANDROID_NDK_HOME="$ndk" ANDROID_NDK_ROOT="$ndk"
  unset CARGO_TARGET_DIR
  timeout 1200 bash ./gradlew --no-daemon --stacktrace --build-cache zipRelease \
    -Porg.gradle.parallel=true -Dorg.gradle.jvmargs=-Xmx2048m
)

python3 - "$source_dir" "$submodule_dir/module/build/outputs/release" <<'PY'
from hashlib import sha256
from pathlib import Path
import struct
import sys
from zipfile import ZipFile

source, releases = map(Path, sys.argv[1:])
archives = list(releases.glob('NyaZygisk-v*-release.zip'))
if len(archives) != 1:
    raise SystemExit('Expected one official BuiltZygisk release ZIP.')
architectures = {'arm64-v8a': ('aarch64', 2, 183), 'armeabi-v7a': ('arm', 1, 40),
                 'x86_64': ('x86_64', 2, 62), 'riscv64': ('riscv64', 2, 243)}
with ZipFile(archives[0]) as archive:
    names = archive.namelist()

    def verified(member):
        checksum_member = member + '.sha256'
        if names.count(member) != 1 or names.count(checksum_member) != 1:
            raise SystemExit('Missing or ambiguous BuiltZygisk payload: ' + member)
        if not 0 < archive.getinfo(member).file_size <= 64 * 1024 * 1024:
            raise SystemExit('Invalid BuiltZygisk payload size: ' + member)
        raw = archive.read(member)
        if archive.read(checksum_member) != sha256(raw).hexdigest().encode('ascii'):
            raise SystemExit('BuiltZygisk output failed its upstream checksum: ' + member)
        return raw

    texts = {'zygisk-sepolicy.rule': verified('sepolicy.rule'),
             'zygisk-module.prop': verified('module.prop')}
    outputs = {}
    for abi, (arch, elf_class, machine) in architectures.items():
        needed = {'zygiskd': (f'bin/{abi}/zygiskd', elf_class, machine),
                  'zygisk-ptrace': (f'lib/{abi}/libzygisk_ptrace.so', elf_class, machine),
                  'libzygisk.so': (f'lib/{abi}/libzygisk.so', elf_class, machine)}
        if abi == 'arm64-v8a':
            needed['libzygisk32.so'] = ('lib/armeabi-v7a/libzygisk.so', 1, 40)
        if abi == 'x86_64' and 'lib/x86/libzygisk.so' in names:
            needed['libzygisk32.so'] = ('lib/x86/libzygisk.so', 1, 3)
        files = dict(texts)
        for output, (member, kind, machine_id) in needed.items():
            raw = verified(member)
            if (len(raw) < 64 or raw[:7] != b'\x7fELF' + bytes((kind, 1, 1))
                    or struct.unpack_from('<H', raw, 18)[0] != machine_id):
                raise SystemExit('BuiltZygisk native architecture mismatch: ' + member)
            files[output] = raw
        outputs[arch] = files

for arch, files in outputs.items():
    dest = source / 'userspace/ksud/bin' / arch
    dest.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (dest / name).write_bytes(raw)
print('Verified official BuiltZygisk assets for: ' + ', '.join(architectures))
PY
