# RDP 0x204：采集暂停与恢复修复

本文保留当时的故障、限时演示及 14:53 反馈。后续声音输出与不限时运行的实现、测试和边界另见[声音与常驻服务报告](AUDIO_SERVICE_2026-11-05.md)，不要将下文“当时未实现”的状态当成最新能力。

环境：VM03，4 核 / 8 GiB，macOS 27.2 / 26B123；宿主 Windows App 11.4.4。重启后 boot UUID 为 E77091D7-9D1D-4497-AA58-CF24117D9D75，rdp UID505 / Aqua 审计会话 100103。没有使用实际 Windows 电脑。

## 故障与修复

原故障是认证成功、发送首帧后约 4.5–5.1 秒结束，客户端报 0x204。录屏和辅助功能仍有效，但采集信号已经停止；输入函数把“合法输入遇到暂停的采集”当成协议失败，关闭整个桥接连接。

现将连接授权与采集可用性分开：

- 采集停止、暂停、空白或超过 3 秒无信号时，清空真实桌面缓存与差异基线，释放已按下的键鼠，发送新生成的暂停提示画面。现有传输与心跳保留。
- 暂停期间仍校验身份、租约、序号及输入格式。合法输入直接丢弃，不能积压到解锁后执行；非法序号、格式、身份、权限撤销及心跳过期仍结束连接。
- 采集状态使用有序的 IPC generation 传递；恢复图像带上 generation。即使 watch 通道把暂停与恢复合并，也必须强制整帧重绘，不能沿用旧差异缓存。
- 采集失去信号后，在同一 GUI 进程中按上限 30 秒的退避重建 SCStream，重新核对本进程窗口、同一 UID / Aqua 会话及权限。会话或本应用重新激活也可触发重试。没有回退到其他用户的物理桌面。
- 重建后的首个完整画面不足以证明恢复：锁定时也可能只返回一帧壁纸。需要后续采集信号确认才恢复输入与画面，期间保留暂停提示。

Apple 的 [SCFrameStatus](https://developer.apple.com/documentation/screencapturekit/scframestatus) 区分 complete、idle、blank、suspended 和 stopped；idle 继续视为活动源，不能把静止桌面直接当成失败。[会话激活通知](https://developer.apple.com/documentation/appkit/nsworkspace/sessiondidbecomeactivenotification) 只用于提示重试，不能作为授权依据。

## 验证证据

本机与 VM03 的实际 Unix socket 传输回归均通过：30 个 1920×1200 完整帧、暂停时发送合法按键而不关闭连接、没有键状态积压、同一租约恢复新的完整帧、重复序号仍关闭。测试不采集桌面，也不注入 OS 键鼠。

新增 Rust 回归覆盖暂停基线清除、只重绘合成提示画面、watch 合并后 generation 仍触发完整重绘，以及 EOF 后不再播放缓存。后续版本另增加半包停顿、硬超时、错误包即时拒绝和输入 generation 回归：25 个应用 Rust 测试、15 个服务端库测试、44 个 Python 测试通过；应用 Clippy 与 git diff --check 通过。

真实 Windows App 已完成：在远端苹果菜单执行锁屏，连接持续保留并显示完整暂停提示；锁定状态下的新连接及按键保持连接，桥日志记录输入丢弃；通过该账号的正常 macOS 解锁后，同一连接自动恢复画面。恢复时显示器 ID 从后台虚拟显示器切为物理显示器 1，原过滤器不能直接复用，重建后恢复。输入另在测试编辑器核对实际文档，不只检查输入发送日志。

初版证据保留在 build/compare/server01.{jsonl,stderr}、server02.{jsonl,stderr}、agent02.jsonl、lock-final-native.jsonl、lock-final-rust.log、lock-vendor.log、lock-python.log 与 lock-clippy.log。初版安装的 GUI 二进制与构建机 SHA-256 一致：799c7c7464627485f3b32a47ffb0d29765c2deab50ee00497d8ac32ae1ad121f；后续版本重新构建，不能沿用此摘要标记后续包。

### 账号切换时的第二条断线路径

初版实际切回 virtualbuddy 控制台时又出现 0x204，服务日志明确记录 bridge_reader_ended / deadline has elapsed。这条 FAIL 保留：源进程在 WindowServer 切换显示会话期间暂停，3 秒 IPC 读超时和 3 秒租约超时把短暂停顿当成永久失败。

后续实现将两种时限分开：采集和输入新鲜度仍为 3 秒；GUI 传输暂时静默时，服务端独立生成暂停提示、停止输入，最多给 30 秒恢复。读取中的同一个包保留到完成，不在软超时后重启解码；超过 30 秒仍结束该连接。权限、身份及协议检查不获得这段宽限。

输入在入队时携带 capture_generation；恢复后的旧 generation 输入丢弃，不会因 GUI 恢复并刷新采集时间而被迟到重放。SCStream 样本按 Apple SDK 的 SCStreamFrameInfoDisplayTime / mach absolute time 验证，超过 3 秒的积压样本不算新采集。原生回归已验证 4 秒停顿保留租约、31 秒静默过期、权限撤销立即关闭、恢复后旧 generation 输入丢弃；Rust 回归验证 8 秒的半包停顿没有破坏消息边界。记录为 transition-rust.log 和 transition-final-native.jsonl；真实三账号切换复测另行核对最终版本。

最终 lockfix05 已在真实 Windows App 连接中执行受限故障注入：只将 UID505 的 GUI 代理 SIGSTOP 8 秒，随后由 finally 恢复。服务日志依次记录 bridge_waiting_for_gui、暂停 generation 3 和恢复 generation 4；仍为 connection 1，没有 multi_transport_ended 或 multi_disconnected，画面自动恢复。另通过远端用户菜单把控制台从 virtualbuddy 切到 vnc 并完成密码解锁，RDP 仍是同一条 UID505 连接；正常输入 rdp05 在 UID505 编辑器内核验，没有落到控制台或 VNC 用户。实际 Windows 操作系统客户端仍未测。

VNC 测试期间有一次密码过早输入到合成编辑器。该测试文档、状态文件及日志的字符内容已清理并验证，保留明确的 credential_redacted 标记；这段被清理的历史不作为最终输入验收证据，最终 VNC 展示使用新的 vnc-final06 测试文档。密码不放入仓库或测试包。

### 最终三个不同账号的同时展示

2026-11-05 14:32 PST，已由代理完成解锁与用户切换，用户无需手动操作。控制台账号核对为 virtualbuddy；Windows App 通过 127.0.0.1:3390 连接 rdp；苹果屏幕共享通过 127.0.0.1:5903 选择“Log in as yourself: vnc”，进入独立桌面。旧的跟随物理控制台的 VNC 连接已关闭，保留一个 VNC 桌面窗口。

最终使用新的 console1105b、rdp1105c、vnc1105b 编辑器，分别通过对应客户端键入 console04、rdp05、vnc06。日志核对三个文档均精确匹配预期，UID / gui_uid 分别为 504、505、506，Aqua 审计会话分别为 100199、100103、100243，三个编辑器均为各自账号的前台程序。三窗口调整大小后再次读取文档，三个核对仍通过。对应证据为 build/compare/demo1105b-input_ownership.json、demo1105b-session_roles.txt、demo1105b-server.jsonl 与 demo1105b-agent.jsonl。

本次展示沿用已验证的程序版本；先前一小时的测试程序已经到时退出，因此重新启动了受限一小时的 demo1105b 采集程序及连接。测试程序与编辑器仍会按时退出，不能把这套展示视为已完成持久化服务。苹果屏幕共享直接使用 macOS 原生服务，本次不经过 ZMacRDS VNC 网关。

## 边界

### 14:53 PST 用户体验反馈复核

用户确认 RDP 流畅度有所改善，同时报告没有声音、VirtualBuddy 黑屏后点击不能唤醒。检查确认音频当前未实现：tools/m1-agent.m 的 capturesAudio=NO，RDP 服务没有配置 SoundServerFactory，config/ZMacRDS-fixed.rdp 的 audiomode=2。不能把音频缺失归因为客户端静音，也不能只改客户端开关就声称已支持音频。后续需接入每个已认证 GUI 用户的音频源与 RDPSND，并验证三个账号的声音不混流、断线后无旧声音重放；完成前继续明确标为不支持。

VirtualBuddy 的实际输入状态为鼠标捕获关闭，这是此前多客户端输入测试后未恢复的设置。现已恢复 VM03 的鼠标捕获，直接在 VirtualBuddy 点击后从黑屏唤醒到 virtualbuddy 登录框，随后使用现有密码正常解锁，整个过程没有使用 VNC 唤醒。为避免全局键盘捕获影响 RDP/VNC，最终仅释放键盘捕获，保留鼠标捕获开启。VNC 仍能看到其 UID506 的 vnc1105b 测试窗口，与 UID504 控制台区分；没有修改来宾锁屏、密码或宿主电源设置。

这个改动解决采集暂停导致连接被误关闭以及恢复基线失效的问题。macOS 账号仍须正常解锁；尚未实现从 RDP 界面提交系统登录密码来解锁独立 Aqua 会话。没有关闭锁屏、绕过密码或修改 TCC。首次图形登录、注销后的新会话、持久化守护与实际 Windows 客户端仍需分别验收。

测试过程中一次远端锁屏快捷键被宿主截获，误锁宿主；用户手动解锁后恢复。后续测试改用远端苹果菜单，没有把宿主锁屏算作来宾测试通过。
