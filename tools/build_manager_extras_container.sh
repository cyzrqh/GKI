#!/usr/bin/env bash
set -euo pipefail
git config --global --add safe.directory "$(dirname "$PWD")"
if [ -f susfs/Makefile ]; then
  make -C "$KDIR" M="$PWD/susfs" ARCH=arm64 CC=clang LLVM=1 LLVM_IAS=1 \
    KCFLAGS="-include $SOURCE_HEADER" modules -j"$(nproc)"
  mkdir -p "$FULL_NATIVE_OUTPUT/aarch64"
  cp susfs/susfs_guard_lkm.ko "$FULL_NATIVE_OUTPUT/aarch64/${KMI}_susfs_guard_lkm.ko"
  llvm-strip -d "$FULL_NATIVE_OUTPUT/aarch64/${KMI}_susfs_guard_lkm.ko"
fi
if [[ " $NATIVE_ABIS " == *" x86_64 "* ]]; then
  module_dir="$PWD"
  export ARCH=x86_64
  unset CROSS_COMPILE
  export KDIR="/opt/ddk/kdir-x64/$KMI"
  cd "/opt/ddk/src/$KMI"
  # Same idempotent x86 preparation used by official Next ddk-lkm-ksu.yml.
  modpost=scripts/mod/modpost.c
  sed -i -E "s/^([[:space:]]*)check_exports\(mod\);/\1\/\/ check_exports(mod);/" "$modpost"
  sed -i -E "s/^([[:space:]]*)s->module = exp->module;/\1\/\/ s->module = exp->module;/" "$modpost"
  sed -i "s/^static void check_exports(/static void __attribute__((unused)) check_exports(/" "$modpost"
  sed -i 's/__version_ext_names\\") =\\n/__version_ext_names\\") = \\"\\"\\n/' "$modpost"
  make O="$KDIR" gki_defconfig
  scripts/config --file "$KDIR/.config" -d LTO_CLANG -e LTO_NONE -d LTO_CLANG_THIN -d LTO_CLANG_FULL -d THINLTO
  if [ "$KMI" = android16-6.12 ] || [ "$KMI" = android17-6.18 ]; then
    scripts/config --file "$KDIR/.config" -e CONFIG_CFI_ICALL_NORMALIZE_INTEGERS
  fi
  make O="$KDIR" -j"$(nproc)" modules_prepare
  make O="$KDIR" -j"$(nproc)" security/selinux/built-in.a
  cd "$module_dir"
  extra=(CONFIG_KSU=m CONFIG_KPM=n KSU_VERSION="$KSU_VERSION"
    KSU_MANAGER_PACKAGE="$MANAGER_PACKAGE" KSU_PACKAGE_NAME="$MANAGER_PACKAGE"
    KSU_EXPECTED_SIZE="$MANAGER_CERT_SIZE" KSU_EXPECTED_HASH="$MANAGER_CERT_SHA256"
    KCFLAGS="-include $SOURCE_HEADER")
  if [ "$KSU_VARIANT" = KernelSU-Next ]; then
    extra+=(KSU_NEXT_MANAGER_SIZE="$MANAGER_CERT_SIZE" KSU_NEXT_MANAGER_HASH="$MANAGER_CERT_SHA256"
      CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER=y)
  elif [ "$KSU_VARIANT" = BakaSU ]; then
    extra+=(CONFIG_KSU_MULTI_MANAGER_SUPPORT=y CONFIG_KSU_TRACEPOINT_HOOK=y)
  fi
  if [ "$KMI" = android17-6.18 ]; then
    make -C "$KDIR" M="$module_dir" MO="$module_dir" -j"$(nproc)" CC=clang compile_commands.json modules "${extra[@]}"
  else
    make -C "$KDIR" M="$module_dir" src="$module_dir" -j"$(nproc)" CC=clang compile_commands.json modules "${extra[@]}"
  fi
  mkdir -p "$FULL_NATIVE_OUTPUT/x86_64"
  cp kernelsu.ko "$FULL_NATIVE_OUTPUT/x86_64/${KMI}_kernelsu.ko"
  llvm-strip -d "$FULL_NATIVE_OUTPUT/x86_64/${KMI}_kernelsu.ko"
fi
chmod -R a+rX "$FULL_NATIVE_OUTPUT"
