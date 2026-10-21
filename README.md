# gki

以 [ABK](https://github.com/xingguangcuican6666/ABK) 为主要构建上游的独立 GKI 构建仓库，参考 [zzh20188/GKI_KernelSU_SUSFS](https://github.com/zzh20188/GKI_KernelSU_SUSFS) 的版本目录补全可选组合。

Actions 中只有一个工作流：**GKI 全属性内核构建矩阵**，同时提供编译和检查上游更新两种操作。

## 构建

进入 **Actions → GKI 全属性内核构建矩阵 → Run workflow**，将“操作”保持为默认的 **编译内核**，再选择参数：

| 参数 | 用法 |
| --- | --- |
| 内核系列 | `all`，或 `5.10,5.15,6.1,6.6,6.12` 中的一个或多个 |
| 指定版本 | 留空构建所选系列全部版本；`6.1.145` 构建该小版本的所有补丁月份；`6.1.145@2025-08` 只构建指定月份。多个目标用逗号分隔 |
| 包含 LTS | 默认关闭，开启后加入所选系列的 LTS |
| 产物模式 | **仅 AK3**（默认），或 **AK3 + boot 镜像** |
| 同时构建数量 | 默认 5，可填写 1–20 的整数 |

保留 ABK 的 KernelSU 变体、分支、SUSFS、ZRAM、BBG、DDK、NTsync、网络增强、KPM、Re-Kernel、虚拟化、自定义模块及内核选项。功能组合的兼容规则继续由 ABK 构建逻辑处理；例如 Official 不启用 KPM，6.12 跳过不兼容的旧 ZRAM 算法。

首次验证建议：内核系列填写 `6.1`，指定版本填写实际需要的一个版本，产物选择“仅 AK3”。确认构建和设备适配后，再按需要扩大矩阵。

### 仅 AK3 模式

仍然编译完整内核，但跳过 boot 镜像的生成、签名、打包以及 App 专用 Bundle。成功后下载名称以 `AnyKernel3-` 开头的 Artifact，其 ZIP 根目录直接包含 AnyKernel3 刷机文件。失败时可能额外提供补丁冲突文件，便于定位原因。

选择“AK3 + boot 镜像”时，Artifact 中包含独立 AnyKernel3 ZIP 和 boot 镜像。Android 12 LTS 的 boot 头补丁日期使用版本目录中最新的 Android 12 稳定补丁月份；文件名称仍标明 LTS。

### 指定 KernelSU 自定义提交

需要固定 KernelSU 的分支、标签或提交时，进入 **Settings → Secrets and variables → Actions → Variables → New repository variable**：

- Name：**`KSU_CUSTOM_REF`**
- Value：所需的分支、标签或提交；也支持上游的 `branch:N` 格式，按上游规则从成功的 CI 记录解析提交。

保存后，在构建参数的“KSU 分支”中选择 **Custom(自定义)**。自定义 ref 从这个仓库变量读取，运行表单不另设输入项。SUSFS 提交固定继续通过 `config/config` 中的 `custom` 和对应 `gki-*` 配置项控制。

### 版本覆盖

初始快照有 **128 个版本与安全补丁月份组合**，开启全部 LTS 后为 **133 个**。同一小版本的不同补丁月份分别保留。后续 ABK 新增的已知组合也会并入矩阵。

| 系列 | 月度组合 |
| --- | ---: |
| Android 12 / 5.10 | 36 |
| Android 13 / 5.15 | 35 |
| Android 14 / 6.1 | 32 |
| Android 15 / 6.6 | 17 |
| Android 16 / 6.12 | 8 |

这些数字表示可选择的源码组合；不代表所有版本和功能组合均已完成实机验证。

## 更新上游

### 自动检查并准备更新 PR

同一个工作流每天北京时间 **08:17** 检查 ABK 和版本目录上游；GitHub 定时运行可能延迟。也可以在默认分支 `main` 上手动运行，将“操作”改为 **检查上游更新**。两种检查都不会启动内核编译，也不会增加另一个 Actions 入口。

只有本仓库实际导入的文件、内核版本目录或生成的构建行为发生变化，工具才会导入更新、重新生成工作流并运行检查和单元测试，随后创建或更新一个待审核 PR。上游 App、网页等未导入部分的变化，以及仅提交记录变化而构建内容未变的情况，不会创建更新 PR。

更新 PR **不会自动合并，也不会自动编译内核**。相同上游候选已有待审核 PR 时，检查会跳过，不重复创建 PR 或发送提醒。PR 会分配给仓库所有者；创建或实际更新 PR 后，GitHub Actions 机器人会在 PR 中 `@` 提醒所有者，避免个人令牌以自己身份创建 PR 时漏掉提醒。是否收到站内或邮箱通知，取决于你的 [GitHub 通知设置](https://github.com/settings/notifications)。合并前请查看文件差异，合并后可手动选择需要的版本验证构建。

更新检查与单元测试不等于所有内核版本或功能组合已编译通过。

### 一次性配置自动更新令牌

只读检查使用工作流自带的 `GITHUB_TOKEN`。更新可能改动 `.github/workflows/` 下的构建文件，需要额外的 **Workflows 写入权限**，因此创建更新 PR 使用单独的 fine-grained personal access token。

1. 打开 [创建 gki 上游更新专用令牌](https://github.com/settings/personal-access-tokens/new?name=gki-upstream-sync&description=Prepare%20upstream%20update%20pull%20requests%20for%20gki&target_name=cyzrqh&expires_in=90&contents=write&pull_requests=write&workflows=write)。链接已预填所有者、90 天有效期和所需权限；在 **Repository access → Only select repositories** 中只选择 **gki**。
2. 确认以下三项仓库权限，不需要其他仓库或账号级权限：

   | 仓库权限 | 级别 |
   | --- | --- |
   | Contents | Read and write |
   | Pull requests | Read and write |
   | Workflows | Write-only 或 Read and write（写入） |

3. 生成后，直接打开 [新增 gki Actions Secret](https://github.com/cyzrqh/gki/settings/secrets/actions/new)，名称填写 **`UPSTREAM_SYNC_TOKEN`**，将令牌粘贴到 Secret 内容并保存。**不要把令牌发到聊天、Issue、PR 或提交到仓库文件。**
4. 回到唯一的构建工作流，手动选择“检查上游更新”运行一次，查看该次运行的摘要。发现可导入的更新但令牌未配置时，检查会停止并提示配置，不会发布更新。

预填令牌 **90 天后过期**，到期前需要续期或重新生成，并将 Actions Secret 更新为有效令牌。预填字段也应在保存前确认；GitHub 的入口说明见 [fine-grained token 官方文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#pre-filling-fine-grained-personal-access-token-details-using-url-parameters)。

### 手动在本地同步

仍可使用 `tools/` 中的同步工具。需要本地 Python 3.10 或更新版本：

```sh
python -m pip install -r requirements.txt
python tools/sync_upstream.py --check
```

确认需要更新后执行：

```sh
python tools/sync_upstream.py --update
python -m unittest discover -s tests -p 'test_*.py'
git diff
git add .
git commit -m "Update upstream build and version catalog"
git push
```

可固定某个提交或分支：

```sh
python tools/sync_upstream.py --update --abk-ref COMMIT_OR_BRANCH --catalog-ref COMMIT_OR_BRANCH
```

工具先导入指定的上游文件，在临时目录重新生成工作流并验证，再更新本地文件及 `upstream.lock.json`。不会自行提交、推送或触发编译。遇到上游结构变化或本地修改过的导入文件时会停止并列出原因，避免覆盖修改。

`tools/`、本 README 和自定义 `config/stock_defconfig` 不由同步工具覆盖。需要长期保留的工作流定制应写在 `tools/generate_workflow.py` 中；直接修改生成的 YAML 会在下次生成时被替换。`.upstream/imported-files.json` 是上游文件校验清单，不要通过重新登记清单来绕过本地修改提示。

只重新生成工作流：

```sh
python tools/generate_workflow.py
```

查看矩阵，不开始编译：

```sh
python tools/matrix.py --kernel-versions 6.1 --targets 6.1.145
```

如已安装 `actionlint`，还可检查生成结果：

```sh
actionlint .github/workflows/kernel-full-feature-matrix.yml
```

## 来源与许可

- 构建逻辑及相关脚本、补丁：ABK，具体提交记录在 `upstream.lock.json`；原 GPL-3.0 许可证保留于 `LICENSE`，第三方说明见 `THIRD_PARTY_NOTICES.md`。
- 版本目录参考：zzh20188/GKI_KernelSU_SUSFS，具体提交同样记录在锁定文件；原许可证保留于 `.upstream/licenses/zzh-LICENSE`。
- 仓库保留必要的 GKI 构建依赖，未导入 ABK App、网页和其他工作流。

本仓库独立维护自己的矩阵与产物设置。上游更新后可按以上流程导入，不依赖 GitHub 的 Fork 同步按钮。
