These are complete, unmodified Android Binder source files. Their original
copyright and GPL notices are retained. `sha256.json` records every file's
SHA256. The small Makefiles contain only the original numeric kernel version
fields used by the compatibility helper; no Binder implementation is mocked.

Official sources, retrieved on 2026-10-24 (Asia/Shanghai):

- `android13-5.15-2022-06`: historical 5.15.41 before the four ordinary vendor
  hooks were added, commit `e0a662bdadd942f289236a25d894afb93ef8de4f`:
  https://android.googlesource.com/kernel/common/+/e0a662bdadd942f289236a25d894afb93ef8de4f/
- `android13-5.15-2025-12`: native 5.15.194 hook contracts and event sites,
  commit `8aa957891694df54ed62ceefe67436a56d115646`:
  https://android.googlesource.com/kernel/common/+/8aa957891694df54ed62ceefe67436a56d115646/
- `android12-5.10-2022-06`: native 5.10.110 hooks, commit
  `737961790eacb78c6ceb83d800cfad2941556b57`:
  https://android.googlesource.com/kernel/common/+/737961790eacb78c6ceb83d800cfad2941556b57/

The 5.10 allocator callback passes `struct binder_alloc *alloc`; 5.15 passes
`size_t *free_async_space`. The latter must point to the actual allocator field
because ReKernel uses `container_of` to recover its allocator. The old 5.10
event precedes the asynchronous space check and leaves zero-size padding after
that check. The historical 5.15 backport preserves the same existing allocator
order instead of importing unrelated allocation changes.

ReKernel callbacks were reviewed at its official commit
`ac08296174d7fb2801c0eee1084f067a34f8a0fe`:
https://github.com/Sakion-Team/Re-Kernel/blob/ac08296174d7fb2801c0eee1084f067a34f8a0fe/LKM-Source/rekernel_binder.c

The regression verifies declarations, real event calls and GPL exports,
preserves already complete native implementations byte for byte, and rejects
unknown callback contracts before modifying any source file. It does not
introduce no-op callbacks, suppress compiler errors, or disable ReKernel.
