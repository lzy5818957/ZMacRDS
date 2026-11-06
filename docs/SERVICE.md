# 常驻 RDP 与声音服务

此模式取代 `start-desktop RUN 3600` 的限时诊断运行。已登录用户的 GUI 代理不设退出时间，由 launchd 管理；网络入口由系统 LaunchDaemon 管理。它仍要求管理员提前建立各账号的 Aqua 图形会话及正常授权，不会在机器冷启动后替用户登录，也不会绕过系统锁屏。

当前实测环境为 VM03：Apple Silicon、macOS 27.2、4 核、8 GiB，两个 RDP 用户加一个控制台用户。macOS Windows App 已接通声音输出。实际 Windows `mstsc`、更多用户及长于本轮的连续运行仍需单独验收，不能据此宣布生产容量已验证。最终安装版本已通过双声道及控制台串音排除、断开后的休眠断言释放和隧道自动重连；前一构建有两路各 61 分 40 秒的长测，构建范围分开记录。具体结果见[声音与服务报告](reports/AUDIO_SERVICE_2026-11-05.md)。

## 安装

在源码树运行 `make m3-kit SIGN_IDENTITY=你的稳定签名身份`，把生成的同架构包带入服务器。安装和升级必须保留同一签名身份及 bundle ID `org.zmacrds.desktop`；临时签名改变后，系统可能要求各用户重新授权。安装器不修改录屏、辅助功能、屏幕共享或自动登录设置。

先按 [M3](M3_TEST.md) 准备 root 持有的私有配置、匹配连接地址的证书及私钥。配置中的用户名必须是对应 UID 的真实本地短用户名；网络口令是独立随机值，不是 macOS 登录口令。示例中的数字、路径和占位口令必须替换。

```json
{
  "broker_uid": 501,
  "certificate": "/private/test/certificate.pem",
  "private_key": "/private/test/private-key.pem",
  "users": [
    {"uid": 505, "credentials": {"username": "rdp", "password": "REPLACE_WITH_RANDOM_RDP_SECRET"}},
    {"uid": 506, "credentials": {"username": "vnc", "password": "REPLACE_WITH_DIFFERENT_RANDOM_SECRET"}}
  ]
}
```

`broker_uid` 必须是存在的普通账号，且与所有桌面账号不同。当前入口默认固定 1920×1200；每条 route 可配置相同的 `width`、`height`，它们必须与实际代理匹配。先让两个用户登录图形桌面，再执行：

```sh
sudo python3 tools/install-service.py \
  --config /private/test/server.json \
  --app './ZMacRDS-Desktop.app' \
  --server './zmacrds-rdp-probe' \
  --launcher './tools/start-desktop' \
  --controller './zmacrds-control' \
  --manager './ZMacRDS-Manager.app'
```

安装位置：

| 内容 | 位置 |
| --- | --- |
| 签名 GUI 程序 | `/Applications/ZMacRDS Desktop.app` |
| 网络程序及启动器 | `/usr/local/libexec/zmacrds-{server,start-desktop}` |
| root 配置、证书与私钥 | `/Library/Application Support/ZMacRDS/`，0700/0600 |
| 网络服务定义 | `/Library/LaunchDaemons/org.zmacrds.server.plist` |
| 每用户服务定义 | `~/Library/LaunchAgents/org.zmacrds.desktop.plist` |
| 每用户运行登记与事件 | `~/Library/Application Support/ZMacRDS/Service/`，0700/0600 |
| 网络日志 | `/Library/Logs/ZMacRDS/server.log`，私有，轮换 |

安装器会正常停止本项目现有服务，替换应用，重新启动服务。当前连接会断开，需要客户端重新连接；用户原来的 Aqua 桌面和应用不会因此注销。旧应用保存在 root 配置目录供管理员核对回退。更新前应停止交互验收，不要在正在进行的长测中升级。

新增菜单栏后台代理及原生管理窗口；安装器的 `--controller` / `--manager` 必须成对提供。账户范围、管理员验证和连接管理见[管理窗口说明](MANAGEMENT.md)。

## 连接与声音

仍只监听 `127.0.0.1:3390`。远端客户端需要经受限 SSH 隧道或等效的已验证安全通道连接，不要直接改成公网监听。Windows App 的“Devices & Audio → Play Sound”选择“On this computer”。[固定分辨率配置](../config/ZMacRDS-fixed.rdp)使用 `audiomode:i:0`，麦克风 `audiocapturemode:i:0`；关闭剪贴板、磁盘、摄像头等未验收通道。声音能力在客户端协商成功后才开启。验证 TLS 指纹与地址，证书更新须重新核对；不要以关闭校验代替证书管理。

音频要求 macOS 14.2 或更新版本。代理使用私有 Core Audio process tap，明确包含本 UID 的输出进程；每次发布前核对内核 UID、进程创建时间和 Core Audio 对象对应关系。它不采集麦克风，也不读取全局混音设备。浏览器等辅助进程只有属于同一用户时才会进入白名单。筛选每两秒刷新，新应用开始播放可能有短暂接入延迟。

每条已认证连接独立协商 RDPSND，传送 48 kHz、双声道、16 位 PCM，约 1.536 Mbit/s/用户，另有协议开销。暂未实现压缩或 UDP 音频。服务端本地硬件在该用户的声音被远程采集时静音；断开后撤销 tap，恢复应用正常本地输出。权限、会话、采集代次或连接租约失效后会丢弃音频；超过 200 毫秒的待发音频也会丢弃，避免慢网络积累旧声音。

不会保存原始音频或桌面像素。常驻模式不启用 M1 的合成输入脚本，也不记录收到的按键文字。事件日志只保留身份、时序、授权与计数等诊断信息，每份最大约 4 MiB，保留当前及两份轮换日志。

## 生命周期与管理

系统服务只在启动阶段使用 root：创建固定 UID relay 后永久降权，再处理网络协议。GUI 代理始终属于各自的用户。网络服务不持有可任意切换 UID 的接口；relay 重新连接时只读自己用户的最新登记，入口仍验证完整身份。

每用户 `open -W` 是 launchd 管理的监护进程，确保通过 LaunchServices 启动签名应用，保留正确的系统隐私权限归属。监护进程退出时 GUI 代理也会退出，避免旧实例占住 socket；launchd 随后重启。每 UID 的锁文件阻止重复代理。服务模式参数为 `service desktop 0 --rdp-bridge --service --audio`，其中 0 明确表示不限时；限时 M1 诊断仍保留 30–3600 秒限制。

```sh
sudo python3 tools/service-control.py status
sudo python3 tools/service-control.py restart --broker
sudo python3 tools/service-control.py restart --desktop rdp
sudo python3 tools/service-control.py stop
sudo python3 tools/service-control.py start
```

普通用户可不加 sudo 管理自己的桌面代理。`stop` 卸载 launchd job，防止 KeepAlive 立即拉回；`start` 重新装载。它不是卸载器；服务文件仍存在，下次相应图形登录/系统启动会按 launchd 设置装载。

单个 GUI 代理故障会断开该用户的 RDP 连接，自动恢复后重新连接即可；另一个用户继续运行。网络入口或固定 UID relay 故障会重启整个网络服务，所有 RDP 传输需要重连，但 GUI 应用仍保留。此模式不宣称客户端能跨所有服务故障无感保持同一 TCP 连接。

真实系统锁定、注销或撤权仍服从 macOS 的安全行为。采集短暂暂停有有界等待，恢复时重绘完整画面并丢弃暂停期间输入；它不是远程密码解锁机制。有效 RDP 连接期间，GUI 代理持有临时的系统空闲休眠抑制活动；断开后释放。它不禁止屏幕熄灭或系统锁屏，也不改宿主电源设置。系统实际休眠时无法继续传输，唤醒后隧道与服务可重新建立连接。

## 可选的宿主常驻隧道

测试用 SSH ControlMaster 关闭后，本机 3390/5903 的临时入口也会消失。仓库另提供可审阅的受限常驻方案，**需要管理员明确选择并安装**，不随服务器安装器隐式创建：

- `tools/install-tunnel-guest.py`：在指定 VM 创建隐藏非管理员专用身份，只允许转发至 VM 回环的 RDP 3390 和原生 VNC 5900；禁用密码登录、终端、执行命令、SFTP、代理转发及反向转发。
- `tools/install-tunnel-host.py`：为当前宿主用户安装自动重连 LaunchAgent；私钥只保存在宿主私有目录，固定服务器 SSH 公钥。按 VM 网卡 MAC 查询宿主 DHCP 租约，地址变化仍必须通过同一主机公钥核对。

先确认 VM SSH 主机公钥、网卡 MAC、专用公钥指纹和转发范围。用 `--prepare-only` 生成宿主脚本/定义供检查，参数说明见两个工具的 `--help`。安装后应实际验证命令/SFTP/未允许端口/反向转发均被拒绝，并验证隧道重启后客户端能重连。不要复制服务器证书私钥或管理员登录口令到宿主 LaunchAgent。

## 复跑声音隔离测试

包内的 `ZMacRDS-Audio-Probe.app` 只生成有限时长的合成声音，不需要录音权限。源码可用 `make audio-probe` 重建。在两个**测试**用户自己的图形会话中分别启动：

```sh
# 用户 A 的 Terminal
open -n ./ZMacRDS-Audio-Probe.app --args 375 90
# 用户 B 的 Terminal
open -n ./ZMacRDS-Audio-Probe.app --args 750 90
```

先准备两个私有检查配置，分别使用该用户的网络凭据、最新 `state.json` 的 UID/nonce、同一已核对的证书，以及 `seconds: 30`、空 `actions`、A 的 `audio_probe: [375,750]` / B 的 `audio_probe: [750,375]`。同时运行两个 `zmacrds-rdp-check`。这会接管相同账号已有的 RDP 控制端，请先退出交互验收；不会关闭用户应用。

声音模式不检查 M1 像素标记，也不发送输入；画面只统计解码更新，不能把它当成本轮重新完成了桌面输入验收。用户归属由服务侧固定 UID 桥接身份和接收侧指定音源/外来频率检查共同核对。检查器分别测左右声道后合成功率，不把两个声道先混合，以免反相信号抵消。它要求音频实时帧数达到测试时长的 70%、至少二十块有效 PCM、本用户频率均值非零、本用户/指定外来频率功率比不小于 40 dB。最终日志给出计数和判定，不包含 PCM。长测可将 `seconds` 设为 3700，并为音源设置略长于目标时长的有限秒数（最大 7200）；测试结束应关闭合成音源。

可再让控制台第三账号播放 1125 Hz，将两个检查配置的第二频率都改为 1125，单独量化排除控制台声音。不要用接收包数或服务端声音协商日志代替非零信号及串音检查。
