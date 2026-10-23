# gki

独立维护的 GKI 内核与配套 SukiSU 管理器构建仓库。构建菜单参考 [ABK](https://github.com/xingguangcuican6666/ABK)，历史版本目录参考 [zzh20188/GKI_KernelSU_SUSFS](https://github.com/zzh20188/GKI_KernelSU_SUSFS)；日常构建和更新不再从这两个仓库同步或导入文件。

构建逻辑由本仓库的 `templates/`、`tools/` 和 `config/` 维护，内核版本来自自包含的 `data/kernels.json`。新增 GKI 版本直接核对 Android 官方源码，SukiSU 与 SUSFS 也直接检查各自官方源。

Actions 只有一个入口：**GKI 全属性内核构建矩阵**，提供 **编译内核**、**仅编译 SukiSU 安装包** 和 **检查上游更新** 三种操作。

## 选择构建

进入 **Actions → GKI 全属性内核构建矩阵 → Run workflow**，将“操作”保持为 **编译内核**，再选择参数：

| 参数 | 用法 |
| --- | --- |
| 内核系列 | `all`，或 `5.10,5.15,6.1,6.6,6.12` 中的一个或多个 |
| 指定版本 | 留空构建所选系列全部版本；`6.1.145` 选择该小版本的所有补丁月份；`6.1.145@2025-08` 只选择指定月份。多个目标用逗号分隔 |
| 包含 LTS | 默认关闭，开启后加入所选系列的 LTS |
| 产物模式 | **仅 AK3**（默认），或 **AK3 + boot 镜像** |
| 同时构建数量 | 默认 5，可填写 1–20 的整数 |

菜单保留 KernelSU 变体、分支、SUSFS、ZRAM、BBG、DDK、NTSync、网络增强、KPM、Re-Kernel、虚拟化、自定义模块及内核配置等选项，兼容规则由本仓库维护。例如 Official 不启用 KPM，6.12 跳过不兼容的旧 ZRAM 算法。

首次验证可选择 `6.1` 和实际需要的一个版本，使用“仅 AK3”，确认编译与设备适配后再扩大矩阵。全部选择超过 GitHub 单个矩阵的 256 项限制时，需要缩小系列或目标范围。

### 构建速度

SukiSU APK 与内核环境准备并行运行；内核在开始编译前读取同一次运行的已签名 APK 身份，核对源码、包名、证书和版本。管理器构建失败时停止配对，发布也要求配套管理器成功。

Gradle 与 Cargo 依赖可跨运行复用；5.10/5.15 保留 ccache，Bazel 系列新增最多 2 GiB 的磁盘缓存。缓存只用于复用构建工作，全部功能配置和产物校验仍会执行。首次构建需要建立缓存，实际耗时还受源码同步与 GitHub 运行机器排队影响，不能保证固定分钟数。

### 下载 Release

成功完成一次内核构建或“仅编译 SukiSU 安装包”操作后，发布步骤会创建对应的 **Release**。在运行摘要中打开 Release，或进入仓库 **Releases** 下载文件：

- 管理器直接提供 `.apk`，名称为 `SukiSU_v<真实版本名>_<真实版本号>-release.apk`，版本信息从实际 APK 读取。
- “仅 AK3”提供可刷入的 AnyKernel3 ZIP，跳过 boot 镜像生成、签名和打包；选择“AK3 + boot 镜像”时提供对应的 AK3 与 boot 产物。
- `SHA256SUMS.txt` 用于核对下载文件。Actions Artifacts 用于任务之间传递文件，ZIP 中转由发布任务处理，日常下载使用 Release。

Release 正文逐内核展示本次实际构建范围、UTC 时间、KernelSU 变体与分支、版本名与版本号、SUSFS 版本、Hook、LTO、BBG 安全补丁、ZRAM、LZ4KD、KPM 及其他功能状态。功能配置从编译出的 `Image` 内嵌配置提取，不把菜单输入或示例值当成编译结果；无法确认的字段标为“未知”。挂载方式由设备上的管理器或元模块在运行时决定，不能仅凭内核构建宣称固定使用 Magic Mount。

Android 12 LTS 的 boot 头补丁日期采用目录中最新的 Android 12 稳定补丁月份，文件名仍标明 LTS。

### 配套 SukiSU 管理器

选择 SukiSU 编译内核时，工作流先确定一个完整的 SukiSU 提交，用同一提交编译 ARM64 管理器及其 `ksud`，签名后将实际包名和签名信息用于本次所有内核。管理器显示名保持 **SukiSU**，独立包名为 **`com.cyzrqh.gki.sukisu`**，可与官方管理器并存，应搭配本仓库同次构建的内核使用。

只需要 APK 时，将“操作”改为 **仅编译 SukiSU 安装包**，选择所需 KSU 分支，无需启动内核矩阵。

签名由仓库统一维护以下 Secrets，并长期使用同一密钥，保证后续版本可继续更新安装：

| Secret | 内容 |
| --- | --- |
| `SUKISU_APK_KEYSTORE` | PKCS12 签名密钥库的 Base64 编码，密钥别名固定为 `gki-sukisu` |
| `SUKISU_APK_PASSWORD` | 密钥库与私钥共用的密码 |

普通运行者正常选择构建参数即可，无需输入或上传私钥。私钥和密码不会放入公开产物；维护者应保留固定签名密钥的备份。

### 源码与 SUSFS 配对

SukiSU **Stable/Dev** 固定为本地适配的提交 `17ff94d9da24d589a9705227fdda39a1c9045698`；开启 SUSFS 时配对 **v2.3.0**，具体记录在 `build-compatibility.json`，五个内核系列分别固定完整的 SUSFS 提交。设置 `config/config` 中的 `custom=true` 时，优先使用显式配置的 SUSFS 提交。

Latest/Custom 及其他 KernelSU 变体按各自选择规则解析，随后将本次源码固定为完整提交。启用 SUSFS 时会检查接口配套，不匹配会提前停止。

**验证状态：** [五系列实测](https://github.com/cyzrqh/gki/actions/runs/39815529229)中，SUSFS v2.3.0 的 5.15.194、6.1.145、6.6.143 和 6.12.92 已完成实际编译及产物校验，并已[公开发布](https://github.com/cyzrqh/gki/releases/tag/gki-39815529229-recovered)。5.10.246 的旧接口与版本校验步骤顺序已修复，正在单独补测。`SukiSU_v4.2.1_41038-release.apk` 已完成实际编译、固定密钥签名和公开下载校验，包括 APK v2 签名及内容摘要。以上属于列出的构建配置验证，不代表所有菜单组合或实机启动均已验证。

### 自定义 KernelSU 提交

在仓库 **Settings → Secrets and variables → Actions → Variables** 设置 `KSU_CUSTOM_REF`，内容为分支、标签或提交，也支持 `branch:N`，从所选分支的成功 CI 记录解析提交。构建时选择 **Custom(自定义)**，表单不另设 ref 输入项。SUSFS 的自定义提交仍由 `config/config` 的 `custom` 与对应 `gki-*` 项控制。

### 版本覆盖

初始独立目录保存 **128 个版本与安全补丁月份组合**，加上全部 LTS 为 **133 个**。同一小版本的不同补丁月份分别保留，后续直接从 Android 官方源补充。

| 系列 | 初始月度组合 |
| --- | ---: |
| Android 12 / 5.10 | 36 |
| Android 13 / 5.15 | 35 |
| Android 14 / 6.1 | 32 |
| Android 15 / 6.6 | 17 |
| Android 16 / 6.12 | 8 |

这些数量表示可选择的源码组合，不代表所有版本与功能组合均已完成编译或实机验证。

## 官方源更新

同一工作流每天北京时间 **08:17** 检查官方源，GitHub 定时运行可能延迟。也可在默认分支上手动选择 **检查上游更新**；检查不会启动内核或 APK 编译。

- **Android GKI 目录：** 直接读取 `android.googlesource.com/kernel/manifest` 与 `kernel/common` 的 Gitiles refs，只纳入上述五组 Android/内核配对的月度分支，核对官方 Makefile 的完整版本，新增行固定为读取时的完整源码 SHA。历史月份、已有 refs 与 LTS 保留在 `data/kernels.json`。
- **SukiSU / SUSFS：** 直接检测 SukiSU 官方提交与发布、SUSFS 官方各 GKI 分支，将新候选记录到 `data/upstream-candidates.json`。**合并候选 PR 只确认已收到更新，不会自动切换 `build-compatibility.json` 中的构建配对。** 更新配对仍需接口适配和真实编译验证。

发现变化时创建或更新待审核 PR；相同候选已有 PR 时跳过重复创建，更新检查不会自动合并或触发构建。PR 会分配并提醒仓库所有者。官方 GKI 目录更新合并后，新目标可供手动选择；SukiSU/SUSFS 的新候选不能视为已经完成自动升级或兼容验证。

Android 目录检查缓存已核实的完整源码 SHA 与版本；未变化的提交直接复用结果，新提交再核对 Makefile。遇到官方接口限流时保留已核实进度，下次检查可接续；其他官方源的成功结果仍可创建更新 PR，运行摘要会明确指出未完成的检查。

### 官方候选 PR 令牌

只读检测使用工作流自带令牌或官方公开接口。**`UPSTREAM_SYNC_TOKEN`** 用于创建官方候选与目录更新 PR，发布步骤也优先使用它，以支持给历史构建创建 Release。原有 Secret 可以继续使用，无需重新配置。日常 PR 只修改 `data/kernels.json` 与 `data/upstream-candidates.json`；令牌保留该仓库的 **Contents、Pull requests、Workflows: Read and write** 权限，其中 Workflows 用于历史构建的发布权限要求。

如在新仓库部署，可使用仅授权该仓库的 fine-grained token，保存到同名 Actions Secret。令牌过期时续期或更新 Secret，勿将令牌放入代码或聊天。

## 本地维护

需要 Python 3.10 或更新版本及 `requirements.txt` 中的依赖。检查官方源与目录：

```sh
python tools/check_official_updates.py --check-only
python tools/update_kernel_catalog.py --check
```

确认需要新增官方 GKI 版本后：

```sh
python tools/update_kernel_catalog.py --update
python tools/generate_workflow.py
python -m unittest discover -s tests -p 'test_*.py'
git diff
```

工具不会自行提交、推送或启动编译。工作流定制应修改 `templates/` 或 `tools/` 后重新生成；直接编辑生成的 YAML 会被下次生成替换。历史 ABK/zzh 快照不参与日常导入与目录合并。

查看矩阵而不编译：

```sh
python tools/matrix.py --kernel-versions 6.1 --targets 6.1.145
```

如已安装 `actionlint`，可检查 `.github/workflows/kernel-full-feature-matrix.yml`。

## 历史来源与许可

独立维护不抹去历史开源来源：构建代码、脚本与补丁曾适配自 ABK 提交 `d43ead425e175356abb66fa273c401ad2af13216`；初始版本目录参考 zzh20188/GKI_KernelSU_SUSFS 提交 `90b76411e8d8ca0417a02bd619996bbd9986fd5f`。这些记录用于来源追溯，不表示继续依赖两个仓库更新。

原 `LICENSE` 保留，目录历史来源的许可保留于 `licenses/zzh-LICENSE`。SukiSU、SUSFS 与其他构建依赖按各自源码文件的许可声明记录，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
