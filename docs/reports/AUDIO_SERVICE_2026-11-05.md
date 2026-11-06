# VM03：RDP 声音隔离与常驻服务

本报告仅记录本轮实际环境：宿主 Mac 上 Windows App 11.4.4、自动 RDP 检查客户端、Apple Silicon VM03（macOS 27.2 / 26B123，4 核、8 GiB）。原生 VNC 与控制台仍使用不同用户。Windows 操作系统客户端尚不可用，因此没有 Windows `mstsc` 验收结论。

## 实现

声音通过 Core Audio 私有、明确包含进程的 tap 采集。进程须同时满足真实/有效 UID 与代理相同；采集发布前继续校验进程创建时间和 Core Audio 对象对应 PID，避免 PID 或对象重用。没有全局排除式 tap、麦克风或物理输入设备。按用户的进程列表每两秒更新。浏览器辅助进程按其内核用户身份处理，不按应用名或 bundle ID 猜测用户。

通过 RDPSND 协商 48 kHz、双声道、16 位 PCM。每条已认证连接独立分配音频状态和有限队列；IPC 音频同时携带 lease、原生采集代次及单调时钟采样时刻。暂停、断开和接管使旧代次失效；写入 RDP 编码器前再检查有效代次及最长 200 毫秒期限。原生待发队列最多五块，RDP 事件最多四块。慢客户端会丢过期声音，不积累长音频。未协商音频的连接不启用 tap；捕获中的本用户声音不再同时发到服务器硬件。

按 UID 包含输出进程及私有 aggregate 的选择参考 [Apple Core Audio tap 示例](https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps)及 [CATapDescription](https://developer.apple.com/documentation/coreaudio/catapdescription)；服务器硬件静音行为对应 [mutedWhenTapped](https://developer.apple.com/documentation/coreaudio/catapmutebehavior)。这些 API 的设计不是隔离验收的替代，下面仍使用实际不同 UID 的并发输出验证。

常驻代理参数为 `service desktop 0 --rdp-bridge --service --audio --guardian PID`，没有原来 3600 秒结束条件。launchd 管理网络入口及每用户的 LaunchServices 监护进程。GUI 代理死亡或监护进程消失后自动拉起；relay 在每次新连接时读取固定 UID 的新登记，完整身份仍须匹配。固定 UID relay 意外退出会让网络服务退出，由 launchd 整体重启，避免保留一个永远不可连接的失效路由。不会注销用户桌面，也不支持绕过系统登录/锁屏。

日志只保留身份、授权、时序和计数，轮换为当前及两份旧文件，每份约 4 MiB；不保存音频、桌面图像或常驻会话的按键文字。M1 的合成输入、脚本、手动断开等文件命令仅用于限时诊断，常驻模式禁用。

## 声音实测

接收端只在内存测合成信号功率，不播放、不写音频文件。声音模式不检查 M1 像素标记，也不发送输入；解码画面更新数量另行记录，不能等同于像素身份或交互验收。各账号运行同一个测试程序，输出不同频率；另一个用户的频率视作污染信号。Hann 窗测量要求本用户均值非零、至少二十个有效波形块，并至少高出另一用户频率 40 dB；不是单凭协商或收到包就判通过。

| 测试 | 时长 | 收到立体声 PCM 帧 | 本用户/另一用户频率功率比 | 结果 |
| --- | --- | --- | --- | --- |
| 两 UID 440/880 Hz，首轮非零输出 | 各 25 秒 | 各约 120 万 | 约 51.32 dB | PASS |
| 最终异步校验、按需启用及服务器静音，UID505 | 30 秒 | 1,412,608 | 62.34 dB | PASS |
| 同轮 UID506 | 30 秒 | 1,314,816 | 54.49 dB | PASS |
| 控制台 UID504 另播 1125 Hz，RDP UID505 播 375 Hz | 30 秒 | 1,433,088 | 87.72 dB（相对 UID506 的 750 Hz） | PASS |
| 同轮 RDP UID506 播 750 Hz | 30 秒 | 1,349,632 | 77.37 dB（相对 UID505 的 375 Hz） | PASS |

控制台轮次证明第三账号播放时两路自己的声音仍正常，但上表功率比只针对各自指定的另一个 RDP 频率；不能把它说成已对控制台 1125 Hz 做了独立量化排除。纯 UID 进程白名单同时从服务侧核对。

Windows App 的现有 RDP 条目已把 Play Sound 从 Never 改为 On this computer。重新连接后，服务记录音频协商，宿主 Core Audio 报告 Windows App 进程正在输出声音。没有录制宿主声音，也没有将这一观察写成真人听音验收。使用的新测试证书 SHA-256 为 `20:A0:86:BE:8D:47:56:FC:A1:A7:25:40:0D:2A:EE:04:09:42:62:A6:58:CD:E8:E7:6D:29:2E:14:72:6D:DD:51`，有效期至 2027-11-05；经用户确认后继续，未勾永久信任。

早期 ScreenCaptureKit 音频方案虽然协商成功、收到 PCM，但全零，记为 FAIL。切换 Core Audio 后首两轮仍为零，继续记 FAIL。随后查询真实 tap UID 并显式设置测试音源 AudioQueue 音量后才得到上述非零结果；因为两项同时调整，不把首轮静音的原因武断归给单一因素。失败日志保留在私有测试目录，不混入成功统计。

## 故障恢复实测

| 注入故障 | 自动恢复耗时 | GUI 会话及其他连接 |
| --- | --- | --- |
| 网络入口 SIGKILL | 约 280 ms | 两 GUI PID、nonce 与审计会话保持；RDP 传输需重连 |
| UID506 固定 UID relay SIGKILL | 约 1072 ms | 网络服务整体重启；两 GUI 保持 |
| UID506 GUI 代理 SIGKILL | 约 1585 ms | 新 PID/nonce，同一审计会话；UID505 长连接继续收音和刷新 |
| UID506 LaunchServices 监护进程结束 | 约 11435 ms | 原 GUI 主动结束，自动启动新代理；UID505 长连接继续 |
| UID505 GUI 暂停 8.008 秒后恢复 | 原连接继续 | 没有 EOF，画面和音频恢复；暂停期间输入不重放 |

GUI 故障轮次客户端出现的 `not enough bytes`/EOF 是故意终止源导致的预期断线，不算正向声音 PASS。恢复后的新连接必须使用新登记并再次收到本账号非零音频。错误凭据轮次要求零画面、零收到的音频包，并核对原有连接不被踢掉。

## 超过一小时验证

两条独立 RDP 连接均完成 3700.002 秒（61 分 40 秒）并 PASS：

| UID | 立体声 PCM 帧 | 音频块 | 本用户/另一用户频率功率比 | 解码画面更新 |
| --- | --- | --- | --- | --- |
| 505 | 175,812,608 | 343,384 | 61.50 dB | 30,788 |
| 506 | 175,952,896 | 343,658 | 65.04 dB | 32,022 |

本轮长测使用检查器当时的左声道频率测量；不是后来双声道检查器的结果。声音模式未检查像素 UID 标记。UID506 后半段由同用户 Safari 的本地 Web Audio 750 Hz 替换 AudioQueue 音源，服务筛选记录确认是该 UID 的 WebKit GPU 进程；没有为浏览器另做一条独立完整短测。

核对代理存活时，UID505 PID14430/nonce3428655033 已运行 4701.007 秒，UID506 PID15006/nonce3575101117 已运行 3708.005 秒，两者与各自基线相同，审计会话保持 100103/100243，权限仍有效。此证据说明代理跨过原 3600 秒退出点；不是单凭反复重启累加时长。外部墙钟与应用计时出现不一致，因此使用客户端 elapsed_ms 和代理单调 uptime 判定，没有以显示的日历时刻估算通过。

长测二进制 SHA-256：服务端 `0511c0a28278d7f76b57a4130d83a67bed5c92df0cb27a2fb82826e0503c7c88`，GUI 可执行文件 `83c86ab27543a852f9754363c00a536db5d18bbe0904d2938d2f45cbad88f6a5`。

## 最终小修补及用户暂停

长测后部署了常驻模式按键诊断禁用、PCM trace 脱敏、网络服务 Interactive 调度及有效连接期间的空闲休眠抑制。没有改变不限时计时条件或音频的 UID 筛选算法。新代理 PID16877/16878，原 Aqua 审计会话保持。最终版本的短回归如下：

- UID505 正确像素标记持续 10 秒，实际发送九个合成文字事件；服务端只记录事件类型与数量，不存在 `input_received` 行或合成文字内容。遗留 `request-disconnect` 文件未被常驻模式执行，测试后已清理。
- UID505 双声道音频 30 秒 PASS：1,365,504 帧，76.90 dB。
- UID506 同轮零音频，FAIL：进程核对发现该账号的合成音源未启动。这轮不能算音频通过，也没有据此推断服务回归；需之后补测。
- 两新 GUI PID 在连接期间均持有 `Active authenticated remote desktop session` 的 PreventUserIdleSystemSleep，系统 PreventUserIdleDisplaySleep 为 0。断开后的释放检查尚未完成。
- 用户正在视频会议，明确要求停止声音测试、晚点再测；已停止全部合成 AudioProbe 进程并关闭浏览器测试标签。不再启动声音、RDP/VNC 交互或窗口操作。

因此最终小修补版本尚未完整完成双声道/控制台 1125 Hz 排除回归，不能用旧版本的长测冒充新版全部验收。最终安装可执行文件 SHA-256 见私有 `build/compare/service-final-build.sha256`，恢复测试时先核对实际安装版本。

专用受限 SSH 身份已获用户确认并在 VM03 安装。RDP/VNC 转发以及命令、SFTP、其他端口、反向转发拒绝均实测；现有三账号的 SSH 有效配置与备份逐项相同。宿主常驻隧道已安装，修复了含空格 known_hosts 路径的 SSH 配置引用；切换后尚未做桌面连接、自恢复故障及三窗口恢复验收。用户会议期间保留后台服务，后续再继续。

## 恢复测试后的最终结果

用户明确恢复测试后，先核对安装文件 SHA-256 与最终小修补版本一致：服务端 `05fb64f06c7c37d5144372eb3631f1cf23de6b1e5eea710d84f41242e379d740`，GUI 可执行文件 `cd9728df5c01d83247b8691f0f7483c2ed27bb35c50d41e4cd2293a5ab40eb4b`。本节补齐上一节因会议暂停而未完成的检查；之前零音频 FAIL 保留，不覆盖原日志。

两个音源分别启动后，核对进程真实 UID、频率、AudioQueue 正在运行和已产生的帧数，再开始客户端测量。控制台轮次还核对第三音源属于 UID504，并在两路 RDP 测量期间确实运行。此轮使用最终双声道检查器，分别测量左右声道，避免右侧遗漏或反相抵消。

| 最终安装版本检查 | 时长 | 收到立体声 PCM 帧 | 本用户/指定外来频率功率比 | 结果 |
| --- | --- | --- | --- | --- |
| UID505，排除另一 RDP 用户 | 30.001 秒 | 1,297,408 | 76.79 dB | PASS |
| UID506，排除另一 RDP 用户 | 30.001 秒 | 1,295,872 | 59.52 dB | PASS |
| UID505，排除控制台 1125 Hz | 30.001 秒 | 1,418,240 | 90.67 dB | PASS |
| UID506，排除控制台 1125 Hz | 30.001 秒 | 1,417,728 | 77.56 dB | PASS |

四项均达到实时帧数至少 70%、本用户非零信号及 40 dB 门槛。两条长测仍是前一版本的 3700.002 秒结果；这里的最终版本没有重新跑一小时，不把两者混为同一构建的长测。

两个 RDP 检查客户端断开后，实际读取系统电源断言，两个 GUI 代理均已释放 `Active authenticated remote desktop session`；屏幕共享或其他系统进程自己的断言不算作本项目未释放。随后实际 Windows App 连接 UID505 时，同一 GUI PID 再次出现相应断言，说明保持唤醒按连接生效，不是全局禁止休眠或锁屏。

常驻隧道进程 PID41486 被单独 SIGTERM 后，launchd 自动启动 PID42466，约 383 毫秒内重新监听宿主回环 3390/5903；VNC RFB 握手通过。此后实际 Windows App 通过常驻入口认证并显示 UID505，原生屏幕共享以“Log in as yourself: vnc”进入 UID506，分别核对了窗口内的 UID 标记。Windows App 在一个有限 8 秒的 UID505 音源期间，其宿主进程 PID42487 的 Core Audio 输出为 running=1。没有录制声音，也没有声称人工听音或音画同步延迟已经验收。

恢复控制台时 VirtualBuddy 一度显示“Shutting Down”遮罩，来宾 SSH、RDP、VNC 仍正常，未把此事当成远程服务错误。用户取消关机后，代理在 VirtualBuddy 的 03 窗口直接点击唤醒、使用已有密码解锁 virtualbuddy；没有重启宿主或虚拟机。最终鼠标捕获为开启、键盘捕获为关闭。控制台内核 UID504，RDP GUI UID505/audit100103、VNC GUI UID506/audit100243；boot UUID 与原来的 GUI 代理 PID/nonce 均保持。另一个任务切换到 02 的画面不作为 03 的测试证据，未对 02 投递来宾输入。

关闭旧维护 SSH ControlMaster 后，再次核对常驻隧道 PID42466 的两个已建立客户端连接均保留，且 RDP/VNC 窗口内 UID505/506 的计数器继续刷新。入口不依赖本次维护终端存活。

三个不同账号窗口已重新打开并分别调整为 RDP 左侧、03 控制台右上、VNC 右下。所有有限测试音源已停止并通过进程检查；保留服务、常驻隧道和已连接的客户端供用户继续体验。

私有复核证据为 `build/compare/audio-resumed-{stereo,console}-{505,506}.log`、`resumed-audio-sources.json`、`service-power-resumed-idle.txt`、`service-power-windows-app.txt`、`tunnel-plan/restart-result.json`、`audio-windows-app-resumed.json` 和 `service-final-status.json`。不将凭据、日志或桌面画面打入分发包。

## 构建和可复跑检查

- Python 3.13：49 项通过。宿主系统 Python 3.9 不支持 VNC 验证器使用的 `asyncio.timeout`，不作为本轮测试运行时。
- 应用 Rust：28 项服务测试、3 项检查客户端测试通过。
- vendored IronRDP：16 项库测试通过，含过期/失效声音丢弃。
- 应用 Clippy `-D warnings` 通过；原生应用 `-Wall -Wextra -Werror` 编译通过。
- 原生 IPC 合成回归宿主与 VM03 均通过：30 个 1920×1200 测试画面，音频 opt-in、PCM 包、暂停丢弃输入、同 lease 恢复、旧代次拒绝、有界 4/31 秒等待、撤权等路径。此测试不录屏或发送真实用户输入。

安装、停止、更新与边界见 [SERVICE](../SERVICE.md)。本轮不包含麦克风、音频压缩、UDP、音画同步延迟量化、Windows 实机、系统密码认证、冷启动创建 Aqua 会话及多人容量验收。
