# M3 同一入口双用户实验

`--multi` 在一个 `127.0.0.1:3390` 监听器上并发处理 RDP 连接，按已验证的独立测试凭据绑定到固定 macOS UID。A、B 不再各占一个端口。当前是预先登录 GUI 账号的实验实现；完整客户端矩阵仍按 [CLIENT_TEST](CLIENT_TEST.md) 验收，实测范围见 [VM03 报告](reports/M3_VM03_2026-11-04.md)。

常驻使用请改按 [SERVICE](SERVICE.md) 安装；下文保留手动、限时 M3 诊断流程。常驻模式自动读取固定 UID 的新代理登记，不再要求代理每次重启后人工改 nonce。声音输出的隔离及服务恢复结果见[本轮报告](reports/AUDIO_SERVICE_2026-11-05.md)。

## 准备

源码树执行 `make m3-kit` 会生成 `build/ZMacRDS-M3-<架构>.tar.gz`，包括同架构 GUI 程序、两个 RDP 二进制、复跑工具和文档，不含运行凭据、私钥或桌面数据。解包后可以用 `./install-m1` 在测试 VM 中安装 GUI 工具（已有代理运行时它会拒绝覆盖），另将 `./zmacrds-rdp-probe` 安装到 root 持有的程序目录。客户端可以直接使用包内的 `./zmacrds-rdp-check`。仅在全新测试安装或确实需要更新 GUI 程序时运行安装器。

1. 按 [M2 操作单](M2_TEST.md)在 A、B 各自的已登录 Aqua 会话启动同一 M1 应用，使用不同 RUN ID 和 `--rdp-bridge`。两账号分别通过系统界面授权录屏和辅助功能。
2. 两个代理的 `bridge.json` 必须分别由对应 UID 持有、权限 0600，所在目录保持私有。当前要求两个桌面的像素尺寸相同，单屏、BGRA；交互模式目标 30 fps，可用 `--rdp-bridge --fps 2` 复现诊断帧率；RDP 库在认证前查询桌面尺寸，不能用首个登录用户的尺寸改变其他连接。
3. 构建 `cargo build --locked`。将程序安装到两个普通账号都能执行、但只有管理员能修改的位置，例如 root 持有的 `/Users/Shared/ZMacRDS-M1/zmacrds-rdp-probe`，权限 0755。程序不能放在仅 root 可进入的配置目录中。
4. 准备与地址匹配的短期 TLS 证书，以及 A、B 各自不同的随机测试密码（16–1024 字节）。这些是本实验的网络凭据，**不是 macOS 登录密码**。

配置文件、证书和私钥由 root 持有、权限 0600，放在 root 持有的 0700 目录。下面是结构示例，UID、路径和口令均需替换：

```json
{
  "broker_uid": 501,
  "certificate": "/private/test/cert.pem",
  "private_key": "/private/test/key.pem",
  "users": [
    {
      "uid": 502,
      "credentials": {"username": "test_a", "password": "REPLACE_WITH_RANDOM_A"},
      "bridge": "/Users/test_a/Library/Application Support/ZMacRDS/M1/m3-a/bridge.json"
    },
    {
      "uid": 503,
      "credentials": {"username": "test_b", "password": "REPLACE_WITH_RANDOM_B"},
      "bridge": "/Users/test_b/Library/Application Support/ZMacRDS/M1/m3-b/bridge.json"
    }
  ]
}
```

启动和停止：

```sh
sudo /Users/Shared/ZMacRDS-M1/zmacrds-rdp-probe --multi /private/test/config.json
# 测试完成后 Ctrl-C；等待服务端和两个 relay 子进程退出。
```

`broker_uid` 必须是存在的普通本地 UID，不能与任何桌面 UID 相同。配置最多 8 个不同 UID、16 条同时建立或活动的连接；这是实验上限，不是已验证容量。不要把口令写进命令参数、日志或版本库。

## 身份与进程边界

启动阶段需要 root 读取配置、创建每用户的 Unix 监听器并启动固定 UID 的 relay。绑定套接字、设置权限期间，0700 临时目录一直由 root 持有；全部绑定完成后才通过已打开的目录句柄移交所有权。随后入口进程永久降到 `broker_uid`，清空其他附加组，并验证不能恢复 root；完成后才监听 TCP。VM03 实测入口为管理账号的 UID 501，两 relay 分别为 UID 502、503。生产专用服务身份尚未部署。

入口按连接保存认证状态、输入队列、画面流和控制权。新连接通过用户名、密码和空 domain 验证后，若同 UID 已有控制端，入口先结束旧传输并释放该用户的键鼠，再把原桌面接给新客户端。错误口令会被拒绝，原连接继续工作。拒绝或关闭 A 的连接不能释放 B 的控制权。即使客户端省略凭据，画面入口仍独立检查认证，未通过时不打开任何代理。

每个 relay 只持有一个固定用户的代理配置，没有网络口令、TLS 私钥或任意 UID 选择接口。入口通过私有 0700 目录内的 0600 socket 访问 relay，relay 检查调用者的内核 UID；relay 再以自己的 UID 连接 GUI 代理，保留 M2 的同 UID 检查。入口核对完整 greeting（UID、审计会话、启动 UUID、随机数、代次、几何及 lease），不会回退到控制台或其他用户。

relay 的 stdin 是私有生命周期 socket；父进程关闭或死亡时 EOF 使其退出。正常退出（Ctrl-C 或 SIGTERM）先等待 relay，再通过原目录句柄清理本轮登记的 socket 名称；替换目录路径不能把清理操作引到别处。SIGKILL 等非正常退出仍可能留下 0700 目录和 socket 节点。

桌面流关闭须返回协议库的终止状态，而不是可重试错误。当前库会立即重试画面错误；错误的关闭方式曾导致一个会话阻塞所有会话，现有回归测试覆盖这一情况。

交互代理保留一个待发送的最新采集画面。当前入口请求完整采集帧，由 RDP 编码器计算变化矩形。采集端局部更新协议已实现，但在 Windows App 实测中出现过黑块，暂不协商开启；不能将该路径记为已通过。读取端按顺序交付画面并对慢客户端施加反压，每次重新登录都会从全屏开始。调试独立实例可以在服务配置中指定回环 `port`（默认 3390，最小 1024）。

交互测试使用 `build/ZMacRDS-Desktop.app`，其独立 bundle ID 为 `org.zmacrds.desktop`。在目标账号中通过系统设置授予录屏和辅助功能权限，同时核对进程心跳中的实际授权状态。临时签名程序重新编译后可能需要重新授权，不能只根据同名旧条目的开关判断新二进制已获准。

## 连接与自动检查

macOS Windows App 的 A、B 两个 PC 条目使用相同地址和端口、不同测试凭据。通过 SSH 访问 VM 时两端都绑定回环地址。不要启用永久证书例外、剪贴板或磁盘共享。普通 M1 诊断默认没有音频；常驻声音测试按 SERVICE 明确启用客户端声音输出，麦克风仍关闭。验证证书身份后按本次测试授权连接。

另外提供只连接回环地址的 RDP 验证客户端：

```sh
cargo build --locked -p zmacrds-rdp-check
build/cargo/debug/zmacrds-rdp-check /private/test/client-a.json
```

客户端配置由当前用户持有、权限 0600、最大 64 KiB：

```json
{
  "certificate": "/private/test/cert.pem",
  "username": "test_a",
  "password": "REPLACE_WITH_RANDOM_A",
  "uid": 502,
  "nonce": 123456,
  "seconds": 30,
  "expect_denied": false,
  "actions": [
    {"at_ms": 2000, "type": "click", "x": 1100, "y": 650},
    {"at_ms": 2500, "type": "text", "text": "synthetic A 中文"}
  ]
}
```

`uid` 与 `nonce` 取自本轮代理注册。客户端用局部证书存储验证名称、有效期和签名，并比较服务端叶证书完整 DER；不会修改系统信任或忽略验证。像素仅在内存解码，识别测试标记的 UID、随机数、CRC 与计数器，发现其他用户标记或 6 秒没有新标记立即失败。只能在识别到预期桌面后发送输入。诊断客户端支持同一回环地址的可选 `port`，以及读取合成动画坐标的 `motion_probe: [x,y]`。持续时间 1–7200 秒，最多 1000 个有序 action；每条文字不超过 64 个 UTF-16 单元。

`expect_denied: true` 的检查不能带输入，只接受明确的 RDP `ServerDeniedConnection` 且零图像更新、零收到的音频包；普通 EOF、TLS 错误或超时不算认证拒绝成功。正向 PASS 仅证明收到正确的新画面并已发送指定事件；还要核对测试编辑器的实际内容和事件标记，不能把“发送成功”当成“输入送达”。

对非活动窗口先发送激活点击，稍后再拖动。验证客户端将鼠标转换拆开发送，间隔 80 ms，避免把整次拖动压成同一瞬间；该间隔是测试驱动设置，不是服务端输入限速。

仓库提供 VM03 固定窗口布局的复跑工具。准备两个上述私有客户端配置（只需本轮的证书、用户名、密码、UID、nonce），再执行：

```sh
umask 077
mkdir -p -m 700 "$HOME/ZMacRDS-lab"
python3 tools/m3-case.py /private/test/client-a.json /private/test/client-b.json \
  "$HOME/ZMacRDS-lab/run-30" --label 30
python3 tools/m3-run.py build/cargo/debug/zmacrds-rdp-check "$HOME/ZMacRDS-lab/run-30"
```

生成器要求父目录 0700，每次创建新的私有目录，拒绝覆盖旧结果。负载持续 1830 秒，100 轮操作分布在整个时段，每轮包含激活、拖动、切换到编辑器、全选与唯一中英文文本。运行器等到两条连接都解码到自己的桌面，再检查未知用户名和错配凭据；两客户端退出后又测试一次错误和交叉口令，避免账号已被占用掩盖认证问题。做出最终判断前，在服务端确认原控制权已释放。

VM03 的前台显示器熄屏曾使该账号停止采集；后台账号继续工作。需要复现专用测试环境时，可在 VM 内为本轮使用限时 `caffeinate -d -i -t 2000`，宿主另行保持唤醒。这样不会改写系统的锁屏设置；熄屏/锁屏造成的断流应单独记录，不能计入持续刷新时长。

两路测试结束后，将客户端日志、两账号的代理/编辑器事件和认证负向日志收集到私有目录，使用 `python3 tools/m3-verify.py results/manifest.json` 复核。manifest 包含 `minimum_seconds`（默认 1800）、至少三个 `negative_logs` 相对路径，以及两个 `routes`。每个 route 的字段如下：

| 字段 | 来源或含义 |
| --- | --- |
| `uid`, `nonce` | 本轮代理身份 |
| `lease` | 本次 `bridge_connected` 事件中的控制权 ID；不要复用其他连接的 ID |
| `client`, `agent`, `editor` | 相对于 manifest 目录的 JSONL 日志路径 |
| `prefix`, `cycles` | 例如 `A29`、100；期望内容为 `A29-000-中文` 至 `A29-099-中文` |
| `actions` | 本轮预期发送 action 数，包含激活、拖动、点击、按键和文字 |
| `drag_from`, `drag_to` | 接收测试窗口内的起止坐标数组，使用 NSEvent 坐标系 |
| `editor_click` | 编辑器窗口内的预期点击坐标数组 |

验收器要求两个完整 lease 在同一服务器启动周期中重叠足够时长，并检查像素标记的刷新间隔、完整文本和输入归属。只统计带该代理标记的拖动和点击；若接收端任一文档快照出现另一用户的负载前缀，即使最后一行正确也会判失败。两条顺序执行的成功连接不能通过并发检查。原始日志和凭据分开保存，日志中的合成测试内容也保持私有。

## 验证和剩余范围

```sh
cargo test --locked -p zmacrds-rdp-probe -p zmacrds-rdp-check
cargo clippy --locked -p zmacrds-rdp-probe -p zmacrds-rdp-check --all-targets --no-deps -- -D warnings
python3 -m unittest discover -s tests -v
```

必须验证同入口双用户、后台输入、错误/交叉凭据、正确身份接管、独立重连和单用户故障隔离；保留事件日志及实际像素标记检查结果。暂不支持系统账号认证、NLA、冷启动创建 Aqua 会话、动态分辨率或多显示器。手动诊断模式使用固定登记，代理重新启动、注销重登或会话代次变化后需要更新配置；SERVICE 常驻模式由固定 UID relay 在每次连接时读取并验证新登记，不复用旧 nonce。网络黑洞、慢客户端与全部修饰键的故障矩阵尚未完整验收。

Windows `mstsc.exe`、两台独立客户端的三种组合、各账号 Codex 工作负载、32 人和生产部署均需后续单独测试。

## 后台服务的验证方式

常驻代理默认不显示诊断标记。请按[后台与管理窗口](MANAGEMENT.md)安装管理入口；输入及隔离回归使用当前测试编辑器的 UID / nonce，并核对两个账号各自的实际文档。只需验证静态桌面连接时，检查器可设 `service_probe: true`、空 `actions`，且不设置音频或动态图模式。该模式只证明收到初始画面并持续连通，不能替代桌面身份与输入隔离测试。
