These complete, small `fs/notify/fdinfo.c` files are official Android GKI
regression inputs. Their original copyright notices are retained.

- `fdinfo-5.15.41-2022-06.c`:
  https://android.googlesource.com/kernel/common/+/refs/heads/deprecated/android13-5.15-2022-06/fs/notify/fdinfo.c
  SHA256 `8439fe0505de6c53164c814d087f302a77aaf3af19f59e782e8caef5da4cf30a`.
- `fdinfo-5.10.110-2022-06.c`:
  https://android.googlesource.com/kernel/common/+/refs/heads/deprecated/android12-5.10-2022-06/fs/notify/fdinfo.c
  SHA256 `01c4d4ff2b9b83353367513d0d0bb9a9b24fef04ca0394e08c865442dc2fca12`.
- `fdinfo-5.15.74-2022-12.c`:
  https://android.googlesource.com/kernel/common/+/refs/heads/deprecated/android13-5.15-2022-12/fs/notify/fdinfo.c

The two v2.3.0 patches contain only the fdinfo section, without alterations,
from the project's existing official SUSFS pins:

- https://gitlab.com/simonpunk/susfs4ksu/-/blob/f43926dc94b8ffbf9b74321313536600f69367b1/kernel_patches/50_add_susfs_in_gki-android12-5.10.patch
- https://gitlab.com/simonpunk/susfs4ksu/-/blob/86b5b10068459dedb545dc5db46a0f0254428eda/kernel_patches/50_add_susfs_in_gki-android13-5.15.patch

The historical v2.0.0 fallback is the real WildKernels patch, Git blob
`eb720f1f6b0f7cbeb01303120a2eed5ead960fc8`, retained to demonstrate that an old
mount-only patch can apply while leaving the v2.3 KSTAT hook absent:

https://github.com/WildKernels/kernel_patches/blob/main/wild/archived/susfs_fix_patches/v2.0.0/a13-5.15/fdinfo.c.patch

All inputs were retrieved on 2026-10-24 (Asia/Shanghai). Tests apply these
actual patches to temporary copies with Git; they do not synthesize the hook
implementations they intend to verify.
