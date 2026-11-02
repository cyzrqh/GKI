#!/usr/bin/env bash
# Build every Android daemon ABI declared by this fixed official source.
set -euo pipefail
source_dir="$1"
tools_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_dir="$(cd "$source_dir" && pwd -P)"
cd "$source_dir"
abis=$(python3 "$tools_dir/manager_full_native.py" abis --source "$source_dir")
export CARGO_TARGET_DIR="$source_dir/target"
for abi in $abis; do
  (
    # Keep NDK/compiler variables local to this architecture and away from AGP.
    if [ "$KSU_VARIANT" = BakaSU ] || [ "$KSU_VARIANT" = KernelSU-Next ]; then
      export ANDROID_NDK_HOME="$ANDROID_HOME/ndk/29.0.14206865"
      export ANDROID_NDK_ROOT="$ANDROID_NDK_HOME"
    fi
    llvm="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64"
    case "$abi" in
      arm64-v8a) arch=aarch64; triple=aarch64-linux-android; compiler_triple=$triple; api=26; machine=183 ;;
      armeabi-v7a) arch=arm; triple=armv7-linux-androideabi; compiler_triple=armv7a-linux-androideabi; api=26; machine=40 ;;
      x86_64) arch=x86_64; triple=x86_64-linux-android; compiler_triple=$triple; api=26; machine=62 ;;
      riscv64) arch=riscv64; triple=riscv64-linux-android; compiler_triple=$triple; api=35; machine=243 ;;
      *) echo 'Unknown official APK architecture; review required.' >&2; exit 1 ;;
    esac
    compiler="$llvm/bin/${compiler_triple}${api}-clang"
    test -x "$compiler"
    export "CC_${arch}_linux_android=$compiler"
    export "CXX_${arch}_linux_android=${compiler}++"
    export "AR_${arch}_linux_android=$llvm/bin/llvm-ar"
    if [ "$arch" = arm ]; then
      export CC_armv7_linux_androideabi="$compiler"
      export CXX_armv7_linux_androideabi="${compiler}++"
      export AR_armv7_linux_androideabi="$llvm/bin/llvm-ar"
    fi
    upper="$(echo "$triple" | tr 'a-z-' 'A-Z_')"
    export "CARGO_TARGET_${upper}_LINKER=$compiler"
    ndk_headers="$triple"
    if [ "$arch" = arm ]; then ndk_headers=arm-linux-androideabi; fi
    export "BINDGEN_EXTRA_CLANG_ARGS_${arch}_linux_android=--sysroot=$llvm/sysroot -I$llvm/sysroot/usr/include/$ndk_headers"
    if [ "$arch" = arm ]; then
      export BINDGEN_EXTRA_CLANG_ARGS_armv7_linux_androideabi="--sysroot=$llvm/sysroot -I$llvm/sysroot/usr/include/$ndk_headers"
    fi
    cargo_cmd=(cargo)
    extra=()
    locked=(--locked)
    if [ "$KSU_VARIANT" = BakaSU ]; then cargo_cmd=(cargo +nightly); fi
    if [ "$arch" = riscv64 ]; then
      # Same pinned toolchain/standard-library build used by upstream Next.
      rustup toolchain install nightly-2026-09-23 --profile minimal --component rust-src
      cargo_cmd=(cargo +nightly-2026-09-23)
      if [ -f .github/riscv64-cargo.toml ]; then extra+=(--config .github/riscv64-cargo.toml); fi
      extra+=(-Z build-std=std,panic_abort)
      # Upstream's build-std adds the pinned Rust standard-library packages to
      # this crate's lockfile. Cargo --locked rejects that native operation.
      locked=()
    else
      rustup target add "$triple"
    fi
    builtins="$($compiler --print-resource-dir)/lib/linux/libclang_rt.builtins-${arch}-android.a"
    test -f "$builtins"
    if [ -f userspace/ksuinit/Cargo.toml ]; then
      echo "Building official $abi ksuinit with all original features."
      RUSTFLAGS="-C target-feature=+crt-static -C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wno-unused-command-line-argument -C link-arg=$builtins" \
        "${cargo_cmd[@]}" build "${locked[@]}" "${extra[@]}" --manifest-path userspace/ksuinit/Cargo.toml --target "$triple" --release
      mkdir -p "userspace/ksud/bin/$arch"
      cp "$CARGO_TARGET_DIR/$triple/release/ksuinit" "userspace/ksud/bin/$arch/ksuinit"
    fi
    echo "Building official $abi ksud with its full native asset set."
    "${cargo_cmd[@]}" clean --manifest-path userspace/ksud/Cargo.toml -p ksud --target "$triple" --release
    RUSTFLAGS="-C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wno-unused-command-line-argument -C link-arg=$builtins" \
      "${cargo_cmd[@]}" build "${locked[@]}" "${extra[@]}" --target "$triple" --release --manifest-path userspace/ksud/Cargo.toml
    daemon="$CARGO_TARGET_DIR/$triple/release/ksud"
    python3 - "$daemon" "$source_dir/userspace/ksud/bin/$arch" "$machine" <<'PY'
from pathlib import Path
import struct, sys
daemon, assets = map(Path, sys.argv[1:3])
raw = daemon.read_bytes()
elf_class = 1 if int(sys.argv[3]) == 40 else 2
if (len(raw) < (52 if elf_class == 1 else 64)
        or raw[:7] != b'\x7fELF' + bytes((elf_class, 1, 1))
        or struct.unpack_from('<H', raw, 16)[0] not in (2, 3)
        or struct.unpack_from('<H', raw, 18)[0] != int(sys.argv[3])):
    raise SystemExit('Official daemon has the wrong architecture')
for path in assets.rglob('*'):
    if path.is_file() and not path.name.startswith('.') and path.name.encode() not in raw:
        raise SystemExit('The daemon omitted an original or paired asset: ' + path.name)
PY
    mkdir -p "$source_dir/manager/app/src/main/jniLibs/$abi"
    cp "$daemon" "$source_dir/manager/app/src/main/jniLibs/$abi/libksud.so"
  )
done
