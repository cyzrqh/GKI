# gki

独立维护的 GKI 内核与配套 SukiSU 管理器构建仓库。构建菜单参考 [ABK](https://github.com/xingguangcuican6666/ABK)，历史版本目录参考 [zzh20188/GKI_KernelSU_SUSFS](https://github.com/zzh20188/GKI_KernelSU_SUSFS)；日常构建和更新不再从这两个仓库同步或导入文件。

构建逻辑由本仓库的 `templates/`、`tools/` 和 `config/` 维护，内核版本来自自包含的 `data/kernels.json`。新增 GKI 版本直接核对 Android 官方源码，SukiSU 与 SUSFS 也直接检查各自官方源。

Actions 只有一个入口：**GKI 全属性内核构建矩阵**，提供 **编译内核**、**仅编译 SukiSU 安装包**、**检查上游更新** 和 **清理历史编译（保留5次）** 四种操作。

## 选择构建

进入 **Actions → GKI 全属性内核构建矩阵 → Run workflow**，将“操作”保持为 **编译内核**，再选择参数：

| 参数 | 用法 |
| --- | --- |
| 内核系列 | `all`，或 `5.10,5.15,6.1,6.6,6.12` 中的一个或多个 |
| 指定版本 | 留空构建所选系列全部版本；`6.1.145` 选择该小版本的所有补丁月份；`6.1.145@2025-08` 只选择指定月份。多个目标用逗号分隔 |
| 包含 LTS | 默认关闭，开启后加入所选系列的 LTS |
| 产物模式 | **仅 AK3**（默认），或 **AK3 + boot 镜像** |
| 同时构建数量 | 默认 20，可填写 1–20 的整数；实际启动数量取决于 GitHub 账号并发额度与可用机器 |

菜单保留 KernelSU 变体、分支、SUSFS、ZRAM、BBG、DDK、NTSync、网络增强、KPM、Re-Kernel、虚拟化、自定义模块及内核配置等选项，兼容规则由本仓库维护。例如 Official 不启用 KPM，6.12 跳过不兼容的旧 ZRAM 算法。

**BBG 防格机、DDK 防格机 LSM、NTsync、网络增强（IPSet + BBR）默认关闭**，需要时可在构建表单中勾选。

构建 SukiSU 时，变体选择 **SukiSU**，分支可以直接选择 **SukiSU 正式版** 或 **SukiSU 最新版**：前者使用官方最新正式 Release，后者使用官方默认分支。APK 和 AK3 中的 SukiSU 使用同一提交和版本号；Android/内核版本仍由“内核系列”和“指定版本”选择。默认已选 SukiSU 最新版。其他 KernelSU 变体继续使用原有四种分支选项。

首次验证可选择 `6.1` 和实际需要的一个版本，使用“仅 AK3”，确认编译与设备适配后再扩大矩阵。全部选择超过 GitHub 单个矩阵的 256 项限制时，需要缩小系列或目标范围。

### 构建速度

“全部”包含目录中的所有历史版本与月份；初始目录有 128 个非 LTS 目标，自动同步后可继续增加，实际数量见运行摘要，不是每个系列只构建一个。任务按 **5.10 → 5.15 → 6.1 → 6.6 → 6.12** 分组生成，每组内的版本和补丁月份从新到旧排列，LTS 位于本组末尾。列表排列不要求逐个编译，默认仍同时构建最多 20 个；实际启动和完成顺序取决于 GitHub 调度及各任务耗时，同账号的其他运行也会占用额度。已开始的运行保持原有任务列表，新排列从下次运行生效。

SukiSU 在展开矩阵前只从官方下载一次完整历史，校验源码与 SUSFS 适配并制作不可变 Git 快照。所有内核和 APK 都从同一快照检出；运行期间官方分支重写也不会导致后半批任务丢失固定提交，且不再逐任务下载移动的 `setup.sh` 或重复拉取 SukiSU。

源码快照优先通过 Actions 缓存传递，同时保留本次运行专用的临时草稿备份；缓存缺失、传输失败或被淘汰时，从备份恢复并核验固定提交，避免全量任务中途缺少源码。缓存和备份绑定同一运行、准备任务的原始尝试次数及完整源码提交；只重跑失败任务时继续使用原备份。正式产物发布并核对完成后，自动删除本次临时备份。编译下载列表中不会出现 `Source-SukiSU-*` 中转压缩包。

草稿备份的恢复复用已配置的 `UPSTREAM_SYNC_TOKEN`，仅传给源码恢复步骤；编译容器不接收令牌。GitHub 默认只读令牌对草稿下载会返回 403，因此全量构建请保持该令牌有效。

同步 Android 源码时，对清单里已固定提交的项目只抓取该提交，避免旧清单额外要求下载 `master`；分支、标签和最终检出的源码版本保持不变。同步日志记录实际下载命令，便于核实网络耗时。

历史 GKI 命名分支移入 `deprecated` 时，只解析同一系列、同一月份的官方分支地址，优先保留仍存在的原引用；明确指定的提交 SHA 和标签保持不变。同步完成后读取实际 Makefile 核对所选完整版本，LTS 核对系列；版本不匹配时在编译前停止。

SukiSU APK 编译并核验成功后才启动内核矩阵；内核在开始编译前读取同一次运行的已签名 APK 身份，核对源码、包名、证书和版本。管理器构建失败时停止配对，发布也要求配套管理器成功。

管理器先按 SukiSU 官方 DDK 流程构建八种 ARM64 KMI 的真实 LKM 模块，模块与 APK 使用同一源码和本仓库的固定公开签名身份。模块使用按源码、证书和 DDK 镜像摘要隔离的缓存传递，不另上传模块压缩包；APK 校验通过后再启动内核矩阵，避免全量内核占满机器后互相等待。

Gradle 与 Cargo 依赖可跨运行复用；管理器另启用 Gradle 编译结果缓存，相同任务可直接复用结果。5.10/5.15 使用压缩的 ccache，先设置同一缓存目录再配置，并在运行摘要输出本次真实命中、未命中统计；Bazel 系列使用最多 2 GiB 的磁盘缓存。缓存只用于复用构建工作，全部功能配置和产物校验仍会执行。首次构建需要建立缓存，实际耗时还受源码同步与 GitHub 运行机器排队影响，不能保证固定分钟数。

使用 `build.sh` 的内核在编译失败后也会保存已完成的 ccache 对象，后续修复可继续复用；源码或补丁准备阶段失败不会保存空编译缓存。历史 6.1 若使用 `build.sh`，采用相同规则。

GitHub 托管机器的磁盘清理由本仓库脚本执行，只删除固定的预装工具目录；不再下载额外删除工具或批量卸载系统软件包。达到 50 GiB 可用空间即可停止，低于 40 GiB 会在同步源码前明确失败；每个目录和整个步骤都有时间上限。该门槛用于提前发现空间不足，不代表所有自定义配置的峰值用量保证。

### 下载 Release

成功完成一次内核构建或“仅编译 SukiSU 安装包”操作后，发布步骤会创建对应的 **Release**。在运行摘要中打开 Release，或进入仓库 **Releases** 下载文件：

- 管理器直接提供 `.apk`，保留所选 SukiSU 官方源码构建生成的实际文件名，仅在签名后去掉 `-unsigned`，例如正式版格式 `SukiSU_v4.2.1-r1_41050-release.apk`。官方开发版也可能生成 `SukiSU_manager-ci_41065-release.apk`；`manager-ci` 来自官方开发标签，`41065` 是实际版本号，不改标成正式版。版本信息从实际 APK 读取，不额外添加 `v` 或改写版本名。
- “仅 AK3”提供可刷入的 AnyKernel3 ZIP，跳过 boot 镜像生成、签名和打包；选择“AK3 + boot 镜像”时提供对应的 AK3 与 boot 产物。
- `SHA256SUMS.txt` 用于核对下载文件。
- 编译页与 Release 使用同一批原文件和文件名：APK、每个目标唯一的 AK3 ZIP、校验文件，以及选用 boot 模式时的独立镜像。APK 与 AK3 自身携带公开构建信息，发布直接复用原文件；编译期间及结束后均不再上传 `Source-*`、`Manager-Identity-*`、`Release-*` 中转或重复包装。故障诊断文件按需保留。

Release 正文用一张“构建配置”总表展示本次所有内核的实际配置：共同配置只列一次，不同配置在同一行注明对应系列或具体版本。表中包含构建范围、UTC 时间、KernelSU 变体与分支、版本名与版本号、SUSFS 版本、Hook、LTO、BBG 安全补丁、ZRAM、LZ4KD、KPM 及其他功能状态。功能配置从编译出的 `Image` 内嵌配置提取，不把菜单输入或示例值当成编译结果；无法确认的字段标为“未知”。挂载方式由设备上的管理器或元模块在运行时决定，不能仅凭内核构建宣称固定使用 Magic Mount。

Android 12 LTS 的 boot 头补丁日期采用目录中最新的 Android 12 稳定补丁月份，文件名仍标明 LTS。

### 配套 SukiSU 管理器

选择 SukiSU 编译内核时，工作流先确定一个完整的 SukiSU 提交，用同一提交编译 ARM64 管理器及其 `ksud`，签名后将实际包名和签名信息用于本次所有内核。管理器显示名保持 **SukiSU**，独立包名为 **`com.cyzrqh.gki.sukisu`**，可与官方管理器并存，应搭配本仓库同次构建的内核使用。

只需要 APK 时，将“操作”改为 **仅编译 SukiSU 安装包**，选择所需 KSU 分支，无需启动内核矩阵。

镜像修补的 KMI 列表来自 APK 中 `ksud` 实际内嵌的 LKM 模块，构建会检查模块、签名和资源名称，拒绝发布空列表的安装包。发布前还用同一源码的镜像修补与恢复程序，对八种模块分别执行合成 boot 格式测试并核对实际写入的模块与 init；该检查不代表实机启动验证。该功能使用官方原生 LKM 模式；SUSFS 2.3 的完整内联功能由本仓库编译的 AK3 / boot 内核提供。

APK 使用固定密钥签署 **v2-only**，签名后同时验证 Android 的安装签名和 SukiSU 内核的管理器认证格式。当前官方内核认证只接受 v2 签名块及其 padding；额外的 v3/v3.1 等签名块会导致证书指纹正确却仍显示“未安装”。正常构建、内核配对和发布都检查最终 APK 字节，遇到不兼容签名块会停止。`tools/repair_manager_signature.py` 可在核对原 41065 APK 摘要、源码和原证书后，用同一固定密钥修复签名格式，保留已编译的程序内容及包名、版本号，无需重编内核。

签名由仓库统一维护以下 Secrets，并长期使用同一密钥，保证后续版本可继续更新安装：

| Secret | 内容 |
| --- | --- |
| `SUKISU_APK_KEYSTORE` | PKCS12 签名密钥库的 Base64 编码，密钥别名固定为 `gki-sukisu` |
| `SUKISU_APK_PASSWORD` | 密钥库与私钥共用的密码 |

普通运行者正常选择构建参数即可，无需输入或上传私钥。私钥和密码不会放入公开产物；维护者应保留固定签名密钥的备份。

### 源码与 SUSFS 配对

SukiSU **Stable(标准)** 跟随官方最新正式 Release，**Dev(开发)** 跟随官方 `dev` 分支（没有该分支时使用官方默认分支），**Latest(最新)** 使用官方默认分支。每次运行开始时解析一次完整提交，随后 APK 与所有内核均使用该提交；不再把这些菜单选项固定为旧的 41038。

SukiSU 正式版/最新版及原有 Stable、Dev、Latest 开启 SUSFS 时配对 **v2.3.0**，五个内核系列的 SUSFS 完整提交及已审核的 SukiSU 内核源码树记录在 `build-compatibility.json`。官方更新保留相同的内核及接口源码树时，可以直接跟随管理器更新；源码树改变会明确停止并提示需要适配，不会静默回到旧版或降级 SUSFS。该文件的 `source_ref` 是适配基线，不是构建版本锁。设置 `config/config` 中的 `custom=true` 时，优先使用显式配置的 SUSFS 提交。

Latest/Custom 及其他 KernelSU 变体按各自选择规则解析，随后将本次源码固定为完整提交。启用 SUSFS 时会检查接口配套，不匹配会提前停止。

历史 5.10/5.15 的 fdinfo 会先适配旧源码上下文，再应用完整 SUSFS 2.3 补丁，并保留原生事件掩码的行为。编译前核对实际 KSTAT 与挂载隐藏功能代码，防止用旧版补丁补了一部分功能后误报成功。

启用 KPM 时，直接使用 SukiSU 官方 KernelPatch **0.13.9** 发布的 `kptools-linux` 与独立 `kpimg`，完整提交、大小和 SHA256 固定在 `config/kpm-release.json`。编译前核验工具与真实 ARM64 载荷，编译后通过标准输入传递超级密码，再核对补丁入口、完整载荷与原始 Image 信息；任何检查失败都保留原始 Image 并停止发布。

旧构建使用的嵌入式 KPM 包装器可能携带空占位载荷并返回成功；来自[旧运行 40062459370](https://github.com/cyzrqh/gki/actions/runs/40062459370) 的 `android12-5.10.236-2025-05-AnyKernel3.zip` 实际把入口指向文件之外。该旧运行的文件不能继续用于刷机，需重新编译，不能仅修改跳转来修复。现在所有最终 Image 在打包与发布时都检查 ARM64 启动入口，但静态检查通过仍不等同于实机启动验证。

[KPM 修复验证 40081347134](https://github.com/cyzrqh/gki/actions/runs/40081347134)已完成 Android 12 / 5.10.236 / 2025-05 的实际编译、完整载荷与入口检查，并[发布修复后的 AK3](https://github.com/cyzrqh/gki/releases/tag/gki-40081347134-1)。SukiSU 为 41065，SUSFS 为 v2.3.0；下载复核确认 AK3 内的 APK 配对信息与完整载荷，文件摘要与 Actions 和 Release 一致，KPM 真实载荷为 294144 字节。维护者已反馈该修复包成功开机；这一反馈只覆盖该设备与构建配置。

旧全量运行中唯一未完成编译的 `5.10.237 / 2025-06` 使用了已迁移的源码分支地址，现已修正并增加同名分支迁移处理及实际源码版本校验。[单版本补测 40085533520](https://github.com/cyzrqh/gki/actions/runs/40085533520)已通过源码同步、完整版本核对、内核编译和产物检查。

**验证状态：** [五系列实测](https://github.com/cyzrqh/gki/actions/runs/39815529229)中，SUSFS v2.3.0 的 5.15.194、6.1.145、6.6.143 和 6.12.92 已完成实际编译及产物校验，并已[公开发布](https://github.com/cyzrqh/gki/releases/tag/gki-39815529229-recovered)。5.10.246 修复后也已[单独补测成功](https://github.com/cyzrqh/gki/actions/runs/39901348783)，包含内核编译、产物校验和 Release 发布。此前的 `SukiSU_v4.2.1_41038-release.apk` 已完成实际编译、固定密钥签名和公开下载校验，包括 APK v2 签名及内容摘要。以上属于列出的构建配置验证，不代表所有菜单组合或实机启动均已验证。

随后 [41050 配套构建](https://github.com/cyzrqh/gki/actions/runs/39936971198)也已成功：SukiSU 提交 `3c6ecb97305d8df8e85593302c8cb8337e763262`、5.10.246、SUSFS v2.3.0，原名 `SukiSU_v4.2.1-r1_41050-release.apk` 与 AK3 已[公开发布](https://github.com/cyzrqh/gki/releases/tag/gki-39936971198-1)。该 APK 的完整摘要、APK v2 签名和固定证书已独立核验。内核 C 编译实际用时 6 分 27 秒；该运行仍使用旧清理步骤，单清理耗时 12 分 50 秒，不能将此耗时视为新清理方案的实测结果。

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

同一工作流每天北京时间 **08:17** 检查并自动同步官方更新，GitHub 定时运行可能延迟。也可在默认分支上手动选择 **检查上游更新** 立即检查。同步会更新仓库数据，不会启动内核或 APK 编译，也不会安装到手机。

- **Android GKI 目录：** 直接读取 `android.googlesource.com/kernel/manifest` 与 `kernel/common` 的 Gitiles refs，只纳入上述五组 Android/内核配对的月度分支，核对官方 Makefile 的完整版本，新增行固定为读取时的完整源码 SHA。历史月份、已有 refs 与 LTS 保留在 `data/kernels.json`。
- **SukiSU / SUSFS：** 直接检测 SukiSU 官方提交与发布、SUSFS 官方各 GKI 分支，将新候选自动记录到 `data/upstream-candidates.json`。SukiSU 构建每次实时解析所选官方来源。SUSFS 的候选记录用于追踪更新，固定配对和内核适配基线的变更仍需接口适配和真实编译验证。

发现变化时自动创建或更新数据 PR，并在归属、修改范围及数据内容校验通过后立即合并，无需手动点 Merge。相同候选已有待处理 PR 时，直接验证并同步该 PR。自动合并只允许 `data/kernels.json` 和 `data/upstream-candidates.json`；保留已有内核记录，新增版本必须固定完整源码 SHA，不修改构建脚本、SUSFS 配对或签名。GitHub 分支规则或合并冲突阻止同步时，运行会报错并保留 PR，供处理后重试。

同步完成后，新增 GKI 版本即可供下次构建选择；APK 和刷机包仍需手动启动构建。工作流不再发送要求手动审核的 @ 提醒评论，GitHub 自身的 PR 通知由账号订阅设置控制。

Android 目录检查缓存已核实的完整源码 SHA 与版本；未变化的提交直接复用结果，新提交再核对 Makefile。遇到官方接口限流时保留已核实进度，下次检查可接续；仅成功检查且通过校验的数据可自动同步，运行摘要会明确指出未完成的检查。

### 自动同步令牌

只读检测使用工作流自带令牌或官方公开接口。**`UPSTREAM_SYNC_TOKEN`** 用于创建并自动合并官方数据更新 PR，发布步骤也优先使用它，以支持给历史构建创建 Release。原有 Secret 可以继续使用，无需重新配置。日常 PR 只修改 `data/kernels.json` 与 `data/upstream-candidates.json`；令牌保留该仓库的 **Contents、Pull requests、Workflows: Read and write** 权限，其中 Workflows 用于历史构建的发布权限要求。

如在新仓库部署，可使用仅授权该仓库的 fine-grained token，保存到同名 Actions Secret。令牌过期时续期或更新 Secret，勿将令牌放入代码或聊天。

## 清理历史编译

在同一个 Actions 入口将“操作”改为 **清理历史编译（保留5次）**，选择默认分支后手动运行。该操作保留最新 5 次已完成的编译运行，删除更早的编译记录、日志及其 Actions 下载产物；进行中的任务、当前清理任务和官方更新检查不参与清理。**Release 中已发布的 APK、AK3 和校验文件保留。**

清理不会在编译完成后或定时检查时自动执行。本地脚本 `tools/cleanup_workflow_runs.py` 默认只预览候选列表，加 `--execute` 才删除；Actions 中显式选择上述清理操作会实际执行删除。运行摘要显示本次保留、删除及失败结果。

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

上述本地检查命令不会自行提交、推送或启动编译；定时工作流按前述规则自动同步官方数据。工作流定制应修改 `templates/` 或 `tools/` 后重新生成；直接编辑生成的 YAML 会被下次生成替换。历史 ABK/zzh 快照不参与日常导入与目录合并。

查看矩阵而不编译：

```sh
python tools/matrix.py --kernel-versions 6.1 --targets 6.1.145
```

如已安装 `actionlint`，可检查 `.github/workflows/kernel-full-feature-matrix.yml`。

## 历史来源与许可

独立维护不抹去历史开源来源：构建代码、脚本与补丁曾适配自 ABK 提交 `d43ead425e175356abb66fa273c401ad2af13216`；初始版本目录参考 zzh20188/GKI_KernelSU_SUSFS 提交 `90b76411e8d8ca0417a02bd619996bbd9986fd5f`。这些记录用于来源追溯，不表示继续依赖两个仓库更新。

原 `LICENSE` 保留，目录历史来源的许可保留于 `licenses/zzh-LICENSE`。SukiSU、SUSFS 与其他构建依赖按各自源码文件的许可声明记录，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
