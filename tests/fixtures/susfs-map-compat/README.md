# Android 6.6 filemap-fold compatibility fixtures

The six C/header files are unchanged Android common sources. `provenance.json`
records each exact commit, original path and SHA256. The July and August trees
both report kernel 6.6.30; July lacks the fold declaration/definition while
August supplies the real iterator-preserving implementation.

- July: `c5bb31908c12dfded18679d8b4a6633c1c073bce`
- August: `394605f7e8266e836a8acf09a8deabd4ab6d7e70`
- Source: <https://android.googlesource.com/kernel/common/>

`susfs23-task_mmu.patch` is the first three unchanged `task_mmu.c` hunks from
the pinned SUSFS 2.3 Android 15 / 6.6 kernel patch, including its OPEN_REDIRECT
fold call, map hiding and KSTAT spoofing branches. The patch is applied with
real Git to both full official task files in the tests. Other SUSFS hunks are
outside this targeted page-size compatibility test.

The repair adds the exact August fold function and its declaration to July's
existing page-size implementation. Tests require the patched task file to
remain byte-identical across repair, including July's original padding output
and `__VM_NO_COMPAT` filtering. On POSIX systems with a C compiler the actual
backported function is compiled and executed for disabled emulation, a fixup
VMA, an ordinary next VMA and end-of-iteration cases.

Android files retain their GPL-2.0 SPDX headers. The SUSFS patch is from
<https://gitlab.com/simonpunk/susfs4ksu> and retains its upstream licensing.
