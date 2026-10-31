本目录保留 CCTV 原始构建配方。来源与固定提交：

- https://github.com/cctv18/oppo_oplus_realme_sm8650/tree/f4e3a8084a7a5f8203e16ea177c8847c7ff517dc
- https://github.com/cctv18/oppo_oplus_realme_sm8750/tree/0d90ea459dd5013ba5b1cb2e1ebc8c6d1544d746
- https://github.com/cctv18/oppo_oplus_realme_sm8850/tree/6c1ffdac3ebf7ebb80254427a7972262b0a8f64e
- AnyKernel3: https://github.com/cctv18/AnyKernel3/tree/091ee586e1a5d5c1c1c957d48fe7c61638d04dc4

22 份配方的原始字节、SHA256、对应镜像仓库、源码提交和实际 Makefile 版本由
`config/oplus-cctv-profiles.json` 记录。内核构建使用配方对应的完整 CCTV 源码树。
20 个 OKI 方案及 2 个通用 GKI 方案分别保留；同一平台类别和完整版本不重复。

本仓库的适配边界：

- 保留官方 SukiSU 和 BakaSU 的独立选择、正式版/最新版和同次签名配套 APK。
  原配方将 SukiSU 转向 BakaSU 的做法不用于此仓库。
- SUSFS 使用官方 2.3 源码与已验证的接口适配。旧版 69_hide_stuff 补丁以旧 SUSFS
  代码为前提，不覆盖当前官方接口。
- KPM 使用当前已验证的载荷和管理器原生控制接口，保留配套检查。
- 工具链、原生 Make 配方、版本命名与 AnyKernel3 的刷写顺序来自对应固定配方。
  保留已有缓存、安全的下载等待、证书/摘要校验与完整内核/Android/KMI 刷入检查。
- 菜单未开放的其它可选功能维持关闭；原始配方作为来源资料保留。
- 全量完整成功才发行，单目标留在 Actions；发行和历史清理规则保持独立。

编译验证与设备开机验证分别记录。Ace 5 16.0.10.500 的完整 HMBIRD 原厂模块适配
仍是独立待办；接入 CCTV 构建方案不代表这项适配已完成。
