# ZMacRDS

**0.3.1 内部测试版（Apple Silicon / macOS 14.2+）**新增局域网 RDP 直连与 Mac 本地密码验证。安装后打开 ZMacRDS Manager，点击“启动服务”，自动识别已有本地用户；客户端输入服务器 IP、短用户名和 Mac 登录密码，不需要逐台建立 SSH 隧道。默认端口3389，界面可查看用户登录、权限和连接状态，可修改连接端口或导出不含密码的 RDP 文件。

首次使用仍须按 macOS 要求完成各账号的系统设置、录屏和辅助功能授权。开启系统屏幕共享并允许账号后，RDP 可通过苹果原生认证按需建立已初始化用户的独立桌面，不保存密码。Windows 操作系统客户端和更大容量仍未实测。见[新版使用说明](docs/QUICK_START.html)与[直连及本地账号验收](docs/reports/LAN_LOCAL_ACCOUNTS_2026-11-06.md)。

以下为0.3.0及此前的实现与测试历史；旧的回环入口和独立口令描述不代表0.3.1点击“启动服务”后的本地账号模式。

可安装交付版本为 **0.3.0（内部测试版，Apple Silicon / macOS14.2+）**。运行 `make release SIGN_IDENTITY=稳定签名身份 VNC_RUNTIME=/absolute/python-runtime` 构建自带运行环境的安装包及交付 ZIP；用户安装和首次配置见[使用说明](docs/QUICK_START.html)，版本验收见[0.3.0交付记录](docs/reports/RELEASE_0_3_0_2026-11-06.md)。

让 macOS 和 Windows 上的远程桌面客户端连接同一台 Mac Server，不同用户可同时操作各自独立的 macOS 图形桌面。支持目标包括全 Windows、全 Mac 和 Mac / Windows 混合连接，32 个并发用户是总容量目标。

首版改以 **VNC 为主要连接路线**：Mac 使用苹果屏幕共享及跨平台 VNC 客户端，Windows 待提供环境后实测。新增回环 VNC 账号网关，复用原生账号认证并实现同账号接管；[运行与打包](docs/VNC_GATEWAY.md)、[验收要求](docs/VNC_TEST.md)。RDP 保留为实验后端，已完成事件驱动桥接、最新完整图像合并和整帧重绘改造；功能连通不代表体验验收达标。

VM03 已同时运行 VirtualBuddy / `virtualbuddy`、RDP / `rdp`、苹果屏幕共享 / `vnc` 三个独立账号，并核对了各自输入，见[三用户测试记录](docs/reports/VM03_THREE_USERS_2026-11-04.md)。Windows VNC 登录独立 macOS 用户桌面尚未实测，不能据此宣称跨平台多用户 VNC 已完成。

依据[开源实现对照与完整链路审查](docs/reports/ARCHITECTURE_REVIEW_2026-11-04.md)的后续实现和结果见 [11 月 5 日改造回归](docs/reports/REFACTOR_VM03_2026-11-05.md)。最终 VNC 网关的双账号 1830 秒交互长测通过，两侧各 100 轮跨应用、中文输入与拖选均与应用日志吻合，接管及慢客户端隔离通过。固定远端 1920×1200、仅缩放客户端窗口时，Windows App 48 次尺寸切换未复现大片黑块；动态远端分辨率尚未实现，开启该选项仍有问题。使用 [RDP 固定分辨率配置](config/ZMacRDS-fixed.rdp)。

锁定与用户切换后出现的 RDP 0x204 另见[采集暂停与恢复修复](docs/reports/RDP_CAPTURE_RECOVERY_2026-11-05.md)：暂停采集不等同于认证失败，暂停输入不会重放；短暂 GUI 停顿保留有界恢复窗口，身份或权限失效仍断开。macOS 系统解锁与完整会话管理仍有独立边界。

最终安装版本已通过双声道双用户与控制台串音排除、断开后释放防休眠状态、常驻隧道自动恢复，并恢复三个不同账号窗口。前一构建另通过两路 61 分 40 秒长测。RDP 声音与常驻运行已接入：按用户筛选应用输出、每条连接独立音频队列，不采集麦克风；服务代理不限时运行，支持 launchd 自动恢复。安装与管理见[常驻服务](docs/SERVICE.md)，验证范围见[声音与服务报告](docs/reports/AUDIO_SERVICE_2026-11-05.md)。这不代表 Windows 实机和生产容量已经验收。

常驻桌面代理已改为菜单栏后台运行，新增原生管理窗口，可查看账号、权限和 RDP 状态，并管理连接与服务。使用方法见[后台与管理窗口](docs/MANAGEMENT.md)，本轮验证见[测试记录](docs/reports/BACKGROUND_MANAGER_2026-11-05.md)。

11月6日后台构建已修正静止虚拟桌面的采集停顿，并通过双账号输入、恢复、声音隔离和固定分辨率窗口缩放短回归。RDP默认兼容策略保留原始位图回退，NSCodec需显式开启试验；Windows实机、远程网络性能及新构建的长时稳定性仍待验收。

**当前进度**　M0 已在 03 虚拟机的两个普通账号中[通过实测](docs/reports/M0_VM03_2026-11-04.md)（macOS 27.2 / 26B123、4 核、8 GiB）。M1 的独立窗口和整桌面采集均已通过双账号前后台实验，见[实测报告](docs/reports/M1_VM03_2026-11-04.md)；[跨应用、中文与本地连接恢复](docs/reports/M1_CROSS_VM03_2026-11-04.md)、注销重登与撤权检查也已完成，VM03 单显示器配置的本地 M1 门槛通过。

实验 RDP 服务端已接通 macOS Windows App 与 VM03 真实桌面，见 [M2 实测报告](docs/reports/M2_VM03_2026-11-04.md)。同一 `127.0.0.1:3390` 入口按测试凭据把 A、B 分别送入自己的桌面。Windows App 双连接、A 重连保留文本与 B 持续工作均已实测。两个自动 RDP 客户端另持续并发 30 分 30 秒，两边各完成 100 轮跨窗口拖动和中英文输入，见 [M3 实验报告](docs/reports/M3_VM03_2026-11-04.md)。当前要求预先登录的 GUI 账号，使用静态测试凭据和回环访问。Windows `mstsc`、完整客户端/故障矩阵、系统认证与 32 人容量尚未完成，不能作为生产远程桌面服务使用。

## 从这里开始

按[双账号测试操作单](docs/M0_TEST.md)运行第一阶段验收。执行 `make m0-kit` 可生成包含代理和验证器的虚拟机测试包。[开发计划](docs/DEVELOPMENT_PLAN.md)列出了后续桌面隔离、两种客户端连接和混合并发的验收条件。

M0 通过后，执行 `make m1-kit` 生成原生采集与输入测试应用，按 [M1 操作单](docs/M1_TEST.md)继续验证。`python3 -m unittest discover -s tests -v` 运行当前验证器与像素解码测试。

通过本地 M1 后，可按 [M2 操作单](docs/M2_TEST.md)使用 `cargo build --locked` 构建回环 RDP 实验服务器，先运行协议动态图，再接入本账号 GUI 代理。同一入口双用户模式和自动验证客户端见 [M3 操作单](docs/M3_TEST.md)；`make m3-kit` 打包同架构的 GUI 程序、RDP 工具和复跑脚本。

若只需查看当前 Terminal 的会话身份，可在装有 Xcode Command Line Tools 的 Mac 上运行以下命令。

```sh
make probe
./build/session-probe
```

工具输出一行 JSON，记录当前进程的 UID、审计会话 ID、Quartz 图形会话和权限预检状态。请在第二个测试账号自己的 Terminal 中重复运行。两个账号输出不同 UID 只能证明取样来自不同账号，画面和输入隔离仍需按计划实测。工具不会创建账号、监听网络、截图或发送键鼠事件。

项目原创代码使用 [MIT 许可证](LICENSE)。仓库内的 IronRDP 服务端及 NSCodec 副本保留上游 MIT / Apache-2.0 许可证，来源与本地修改见[服务端补丁](vendor/ironrdp-server/ZMACRDS_PATCH.md)和[NSCodec 补丁](vendor/ironrdp-nscodec/ZMACRDS_PATCH.md)。
