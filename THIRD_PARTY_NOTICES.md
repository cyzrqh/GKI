# 第三方来源与许可

本文记录本仓库的历史开源来源、保留代码及构建时使用的源码。具体许可文本以对应仓库的 `LICENSE`、`COPYING` 和文件内声明为准。

## 历史来源

| 来源 | 历史提交 | 本仓库中的用途与记录 |
| --- | --- | --- |
| [xingguangcuican6666/ABK](https://github.com/xingguangcuican6666/ABK) | `d43ead425e175356abb66fa273c401ad2af13216` | 菜单参考及历史构建代码、脚本和补丁的适配来源；原 GPL v3 文本保留于 `LICENSE` |
| [zzh20188/GKI_KernelSU_SUSFS](https://github.com/zzh20188/GKI_KernelSU_SUSFS) | `90b76411e8d8ca0417a02bd619996bbd9986fd5f` | 初始内核版本与补丁月份目录的历史参考；原 GPL v2 文本保留于 `licenses/zzh-LICENSE` |

本仓库当前独立维护 `templates/`、`tools/`、`config/` 与 `data/kernels.json`，不再从 ABK 或 zzh 仓库同步、导入构建更新或合并版本目录。历史出处和署名保留；ABK Android App、网页及其依赖清单不属于本仓库构建内容。

## 当前官方源码

| 项目 | 官方来源 | 可核实的许可记录 |
| --- | --- | --- |
| Android GKI 内核及 manifest | [kernel/common](https://android.googlesource.com/kernel/common)、[kernel/manifest](https://android.googlesource.com/kernel/manifest) | 按各官方仓库及文件中的 `COPYING`、`LICENSE`、SPDX 声明 |
| SukiSU | [SukiSU-Ultra/SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | 当前适配提交的根目录 `LICENSE` 为 GNU GPL v3，`kernel/LICENSE` 为 GNU GPL v2；其他文件按其自身声明 |
| SUSFS | [simonpunk/susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu) | 当前采用源码的 `LICENSE` 为 GNU GPL v3；保留具体文件的原有声明 |

GKI 目录更新直接核对 Android 官方 Gitiles refs 与 Makefile，新增条目固定完整源码提交。SukiSU/SUSFS 官方更新检查只记录候选；实际构建采用的兼容配对记录在 `build-compatibility.json`。

## 保留的本地代码声明

| 文件或组件 | 原有声明 |
| --- | --- |
| `ddk/xingguang-ddk/xingguang_ddk.c` | `MODULE_LICENSE("GPL")` |
| `ddk/patches/xingguang-ddk/0001-xingguang-ddk-api.patch` 中新增头文件 | `SPDX-License-Identifier: GPL-2.0` |
| `zram/lz4/Makefile` | `SPDX-License-Identifier: GPL-2.0-only` |
| `zram/include/linux/lz4.h` | `SPDX-License-Identifier: BSD-2-Clause` |

其他本地模块、补丁和源码文件保留各自的版权及许可声明。

## 构建依赖与 APK 内容

构建还会按所选功能读取 [AnyKernel3](https://github.com/WildKernels/AnyKernel3)、[WildKernels/kernel_patches](https://github.com/WildKernels/kernel_patches)、[ShirkNeko/SukiSU_patch](https://github.com/ShirkNeko/SukiSU_patch)、[Numbersf/Action-Build](https://github.com/Numbersf/Action-Build)、[Baseband Guard](https://github.com/vc-teahouse/Baseband-guard)、[Re-Kernel](https://github.com/Sakion-Team/Re-Kernel) 和 [Droidspaces-OSS](https://github.com/ravindu644/Droidspaces-OSS) 等项目，以及 Android/LineageOS 构建工具链。这些依赖的许可说明应查阅实际检出版本中的许可文件与版权声明。

配套 ARM64 SukiSU APK 从选定的 SukiSU 提交编译管理器、JNI 与 `ksud`，该版本需要时同时编译 `ksuinit`。其 Rust、Android 和 Gradle 依赖由同一源码提交中的 `Cargo.lock`、`Cargo.toml`、`manager/gradle/libs.versions.toml` 等声明；本仓库未提交预编译的 `ksud`。

APK 显示名为 SukiSU，包名为 `com.cyzrqh.gki.sukisu`，由本仓库固定密钥签名。源码提交与实际包信息随构建元数据记录，公开 Release 不包含签名私钥或密码。

## 历史署名

以下署名沿用历史引入说明，具体贡献范围以原项目历史为准：

[@Akuma-Noko](https://github.com/Akuma-Noko), [@DebugBoard](https://github.com/DebugBoard), [@DreamFerry](https://github.com/DreamFerry), [@elysias123](https://github.com/elysias123), [@fanziyun](https://github.com/fanziyun), [@Fede2782](https://github.com/Fede2782), [@FixeQyt](https://github.com/FixeQyt), [@FunLay123](https://github.com/FunLay123), [@gsf114](https://github.com/gsf114), [@guruji-byte](https://github.com/guruji-byte), [@huime180](https://github.com/huime180), [@liqideqq](https://github.com/liqideqq), [@LX200944](https://github.com/LX200944), [@Mazha0309](https://github.com/Mazha0309), [@MiRinChan](https://github.com/MiRinChan), [@prpjzz](https://github.com/prpjzz), [@ReeViiS69](https://github.com/ReeViiS69), [@ShirkNeko](https://github.com/ShirkNeko), [@Starsun](https://github.com/Starsun), [@TheSillyOk](https://github.com/TheSillyOk), [@TheWildJames](https://github.com/TheWildJames), [@Tools-cx-app](https://github.com/Tools-cx-app), [@ukriu](https://github.com/ukriu), [@wrnxr233](https://github.com/wrnxr233), [@Xiaomichael](https://github.com/Xiaomichael), [@xingguangcuican6666](https://github.com/xingguangcuican6666), [@yx1234587](https://github.com/yx1234587), [@zzh20188](https://github.com/zzh20188).
