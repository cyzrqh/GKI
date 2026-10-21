# gki

以 [ABK](https://github.com/xingguangcuican6666/ABK) 为主要构建上游的独立 GKI 构建仓库，参考 [zzh20188/GKI_KernelSU_SUSFS](https://github.com/zzh20188/GKI_KernelSU_SUSFS) 的版本目录补全可选组合。

Actions 中只有一个工作流：**GKI 全属性内核构建矩阵**。

## 构建

进入 **Actions → GKI 全属性内核构建矩阵 → Run workflow**，选择参数：

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

同步工具在 `tools/` 中，不会增加 Actions 入口。需要本地 Python 3.10 或更新版本：

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
