# M2 RDP 实验操作单

本文记录单用户 M2 模式；同一入口双用户实验见 [M3 操作单](M3_TEST.md)。这是受控测试服务器，尚不是多用户产品。M0/M1 通过范围见报告。当前基于 crates.io 发布的 `ironrdp-server = 0.13.0`，使用仓库内副本修复绝对鼠标点击包的坐标丢失问题，修改及来源见[补丁说明](../vendor/ironrdp-server/ZMACRDS_PATCH.md)。其余依赖由 `Cargo.lock` 固定；未使用 GitHub 主分支的未发布版本。

macOS Windows App 已通过协议动态图和 VM03 单账号真实桌面的首次接入，包含跨窗口点击、拖选、服务端拼音和重连保留文本，见[实测报告](reports/M2_VM03_2026-11-04.md)。完整 M2 与多用户 M3 仍未通过。

## 构建与协议探针

```sh
cargo build --locked
cargo test --locked
cargo clippy --all-targets --no-deps --locked -- -D warnings
cargo test -p ironrdp-server --no-default-features --features helper coordinate_tests --locked
make m1-kit
```

Rust 产物位于 `build/cargo/debug/zmacrds-rdp-probe`。准备短期、具有 `serverAuth` EKU 和准确 SAN 的测试证书；使用独立随机测试密码，不使用宿主或 VM 管理员密码。凭据文件格式为 `{"username":"测试名","password":"至少16字节随机口令"}`，凭据文件和私钥均须由运行用户持有、权限 0600。文件放在 0700 私有目录，不能提交到版本库。

Clippy 的 `--no-deps` 将严格告警检查限定在本项目 crate；上游副本在当前禁用可选功能的组合下有一条 `rest_pat_in_fully_bound_structs` 告警。补丁使用上面的独立回归测试验证。

```sh
zmacrds-rdp-probe cert.pem key.pem credentials.json
```

只监听 `127.0.0.1:3390`，只提供 TLS，不降级为明文 RDP。动态测试图为 1024×768、5 fps，展示帧数和输入计数。输入只更新测试图，不注入操作系统。服务端独立维护认证门槛，缺少 ClientInfo 凭据时也不能获取画面或输入；错误凭据会清除认证状态。没有开启 TLS 密钥日志、剪贴板、音频、磁盘或打印机重定向。

Windows App 添加 PC，地址为 `127.0.0.1:3390`，凭据选 Ask when required，关闭全屏启动和动态分辨率；本阶段选择固定远程尺寸并由客户端缩放。关闭设备、音频和剪贴板共享。不要直接信任未知证书；记录并核对 SHA-256 指纹。当前实测使用一次性同意的自签名测试证书例外，不代表完成受信任证书部署。

## 接入一个真实 GUI 会话

只在已通过 M1 的普通测试账号 GUI 会话内启动代理。与 M1 常规模式相比，需要显式添加 `--rdp-bridge`；不加此参数时不会创建桥接套接字。

```sh
open -n /Users/Shared/ZMacRDS-M1/ZMacRDS-M1.app --args m2-case desktop 3600 --rdp-bridge
```

通过正常系统设置为同一应用启用录屏和辅助功能。临时签名的可执行文件变化后，系统可能要求移除旧条目、添加当前应用并重新启动；不得直接修改 TCC 数据库。每次启动使用新 RUN ID。

代理会在本用户私有 RUN 目录中写出 `bridge.json`，包含 UID、审计会话、启动 UUID、代理随机数、代次、几何及套接字路径。它不包含密码，也不能作为网络登录凭据。**RDP 程序必须以该普通用户身份运行**，然后传入这个文件：

```sh
zmacrds-rdp-probe cert.pem key.pem credentials.json \
  "$HOME/Library/Application Support/ZMacRDS/M1/m2-case/bridge.json"
```

网络密码只认证当前这一份实验配置，不是 macOS 系统账号认证。服务端将已认证的连接固定绑定到启动时指定的单个代理。此单用户模式不进行多用户路由；多用户实验须使用 `--multi`，不能用多开不同端口代替 M3 验收。

若客户端位于宿主机，可通过既有 SSH 连接转发 VM 的回环端口。两端都明确绑定回环地址，不开放宿主或 VM 的 LAN 监听。TLS 仍在客户端和 VM RDP 程序之间协商。

## 本地 IPC 边界

- 0700 临时目录、0600 Unix socket；双方检查内核提供的 peer UID。代理仅允许一个相同 UID 控制端。
- 每次接入分配新 lease。控制消息必须逐条携带完整会话身份、lease 与严格递增序号；拒绝旧会话和重放。
- 代理定期复查 GUI 会话和权限；停止、断线或心跳超时清除控制权并释放已按下的键和鼠标。注销后的不同会话不投递清理事件。
- 图像只在内存传输，BGRA、单显示器、当前 M1 的 2 fps 基线。发送端至多一帧等待写入，接收端仅保留最新帧；不创建无限队列。
- 输入队列有界，超限关闭输入通道，避免静默丢失 key-up。Unicode 高低代理对组合后再发送。Windows 键映射 Command，Alt 映射 Option；不支持的扫描码忽略。
- 当前捕获代次一旦改变，需要重新启动/配置桥接实验；不能自动复用旧注册进入新会话。

以同一普通用户运行无 OS 输入注入的边界检查：

```sh
zmacrds-rdp-probe --check-bridge \
  "$HOME/Library/Application Support/ZMacRDS/M1/m2-case/bridge.json"
```

此检查实际连接代理，校验一帧的尺寸，尝试第二控制端、重复序号、错误审计会话和超大控制消息，并等待心跳过期。不保存像素。运行时不能已有 RDP 控制端占用代理。

## 尚需验收

按 [CLIENT_TEST](CLIENT_TEST.md) 验证真实桌面的坐标、修饰键、中英文、断线重连、锁屏与权限边界；在 Windows `mstsc.exe` 重复。动态分辨率、多显示器、可靠 NLA/系统身份认证、完整多用户客户端矩阵、容量与生产稳定性未完成。协议图跑通或单次 IPC 检查通过，均不等于 M2/M3 完成。
