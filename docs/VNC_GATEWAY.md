# 有界 VNC 网关实验

本实现复用 macOS 原生屏幕共享，以系统本地账号认证并选择已登录的独立桌面。网关按账号保持一个控制端：新的正确登录接管旧端，错误口令不影响旧端，其他账号继续运行。它不创建 Aqua 会话，不修改 TCC，不包含系统密码，也不开放局域网端口。

## 环境与运行

先在目标 Mac 正式启用屏幕共享、授权指定本地用户，并让各用户完成首次图形登录。服务端运行于普通账号；监听固定为 `127.0.0.1`，支持 1–8 个预配置账号、最多 16 条未完成连接、最长 7200 秒实验。使用独立受限目录保存配置和私钥，目录 0700、文件 0600，归运行账号所有。复制 `config/vnc-gateway.example.json` 并填入实际路径、短用户名、UID，以及通过可信本地/SSH 通道核对的原生服务器 RSA 公钥 DER 和 SHA-256。占位配置不能运行。

```sh
./python/bin/python3 app/vnc_gateway.py /absolute/private/path/config.json
```

网关会生成独立的私有 RSA 密钥，以及私有目录中的 DH 参数缓存，输出回环端口及公钥摘要。初始化认证总限时 90 秒，包含人工填写凭据的时间。不会安装后台常驻服务。客户端通过已有、已验证宿主密钥的 SSH 连接转发网关端口，例如本地 5907 → 服务端 127.0.0.1:5901，再连接 `127.0.0.1:5907`。客户端必须支持 Apple 用户名/密码认证（RSA 33 或 DH 30）；不支持无认证或传统 VNC 共享密码回退。

Apple 账号握手只保护凭据，**后续 RFB 画面和键鼠流本身没有加密**。所有跨机器链路须放在 SSH 内。上游原生 RSA 公钥在发送任何凭据之前核对，发生替换立即拒绝；不要把一次连接后才检查摘要当作事前身份验证。DH 为兼容路径，不能据此宣称端到端现代 TLS。

当前通过的 GUI 客户端是 TigerVNC 1.16.2（DH 认证、ZRLE、固定尺寸）。Apple Screen Sharing 的 3.3 握手未通过当前网关兼容性；它直连原生服务的结果不代表网关通过。Windows 客户端未测。

上游先请求控制模式并等待 2.2 秒初始化。首次完整视口请求之后启用 Apple 原生变化推送，吸收重复的全视口增量请求，避免请求队列；部分视口回到轮询。仅接受 32 位 true-color 及可解析的 raw / CopyRect / zlib / ZRLE、桌面/光标伪编码。压缩像素原样转发，不重新编码。一个完整更新限时 1 秒、64 MiB / 4096 矩形；慢客户端断开后可以重连，不能靠丢差异补丁继续。会话或远端几何改变会关闭连接。剪贴板消息上限 64 KiB；测试 GUI 关闭双向剪贴板。

当前只是受控实验入口：首次登录、锁屏/注销、原生后端故障、公开网络、常驻守护和完整客户端矩阵还要独立验收。直接连接原生 5900 会绕过网关的接管策略，应在正式服务设计中统一入口和访问边界。

## 可重现检查

`app/vnc_check.py` 使用私有 JSON 配置，持续读取 UID、nonce、tick、CRC 像素标记；不注入输入，不写桌面截图。`app/vnc-input-check.py` 仅在项目编辑器的预期 UID/nonce 标记存在时发送固定 30 个 ASCII 字符及四个中文 Unicode keysym，并等待文档长度的可见像素回显。它会接管同账号的观察连接，须在持续观察结束后运行。键鼠和中文是否有效，以编辑器保存的文本、身份和事件记录再次核对。

观察器等待增量更新超时后请求一次全画面，保留正在解码的同一消息，避免中断半包。它记录恢复次数和故障类型；有恢复的通过不能写成零停顿。观察到的不同 tick 数量不是 GUI 最终呈现 FPS。

`app/vnc-takeover-check.py A.json B.json` 核对错误密码不踢旧端、正确新连接接管、另一账号保持更新；`app/vnc-slow-check.py A.json B.json` 暂停 A 读取并验证移除和恢复。它们会接管测试账号，必须先结束同账号的长测。

长测：配合 M3 包中的两个编辑器 bundle 和 `m3-motion NONCE 3600 --corner`，分别在两个已登录测试账号中启动。主编辑器用 `open -n … --args RUN_ID 3600`，辅助编辑器加 `--secondary`。在远端固定 1920×1200 下，主文档标记 `[924,406]`、移动标记 `[48,1098]`；测试期间不要移动这些合成窗口。每个账号各运行：

```sh
./python/bin/python3 app/vnc-workload-check.py /private/account.json --motion-nonce MOTION_NONCE --prefix UNIQUE_PREFIX >account-client.jsonl
```

配置严格包含 `port, username, password, uid, nonce, marker, seconds, host_key_sha256`；`nonce` 为主编辑器启动日志的 nonce，`marker` 为主文档坐标，`seconds` 为 1830。配置必须归当前用户所有且 0600，公钥摘要经可信通道核对。测试会清空合成编辑器，不能对普通应用运行。两账号使用不同 prefix/nonce。运行 `app/vnc-workload-verify.py CLIENT PRIMARY_LOG SECONDARY_LOG --uid UID --other-prefix OTHER_PREFIX --since-uptime-ms GUEST_START_MS`，对照应用日志核验全部 100 次文本及选区、两应用点击和拖动。开始时间必须使用来宾 `CLOCK_MONOTONIC_RAW`，不能直接混用宿主单调时钟。完整结果、延迟口径与已知失败见[回归报告](reports/REFACTOR_VM03_2026-11-05.md)。

## 打包

在构建机准备独立 Python 运行时、安装 `tools/vnc-test-requirements.txt`，再执行 `tools/package-vnc /absolute/path/to/python`。输出位于 `build/ZMacRDS-VNC-arm64.tar.gz`。包仅含代码、运行时及其许可证、示例配置和文档，不含测试账号口令、原生服务器 pin 或网关私钥。依赖版本、原始项目和许可证见 [VNC_THIRD_PARTY.md](VNC_THIRD_PARTY.md)，Python 依赖不是全部采用本项目 MIT 许可证。运行时来自官方 Astral python-build-standalone 时，应核对所选版本的官方校验和；本轮实测版本及摘要见测试报告。

## RDP 实验客户端

`config/ZMacRDS-fixed.rdp` 固定远端 1920×1200，启用客户端窗口缩放，关闭动态远端分辨率。当前没有实现 Display Control 动态分辨率协商，不能启用 Windows App 的 “Update the session resolution on resize”。这个配置不保存凭据、不建立永久证书信任。客户端设置的含义见 [Microsoft RDP 属性](https://learn.microsoft.com/en-us/azure/virtual-desktop/rdp-properties)。Windows App 采用扫描码输入；服务端拼音与直接 Unicode 输入分别测试，不能互相替代。

桌面代理应由已登录的本用户执行 `tools/start-desktop`，通过 LaunchServices 打开已签名的 `.app`。不要从 root SSH 会话直接执行 `Contents/MacOS/M1Agent`：本轮发现那样重新启动后录屏预检变为 false，而同一签名、同一路径使用 `open -n … --args …` 后实际采集与两项权限均恢复，无需重新授权。
