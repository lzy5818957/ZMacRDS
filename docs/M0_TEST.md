# M0 · 两个已登录账号的会话身份测试

在同一台 macOS 虚拟机内，用快速用户切换保持两个普通账号同时登录。测试记录两个代理的进程 UID、图形会话 UID、审计会话、前台账号和权限预检结果。M0 的验收范围是会话身份，桌面采集与键鼠隔离在 M1 验收。

## 准备

预编译包文件名包含架构，例如 Apple Silicon 对应 `ZMacRDS-M0-arm64.tar.gz`，构建目标为 macOS 13 或以上。在虚拟机的管理员账号下执行以下命令。新目录归创建者所有，两个测试账号只需读取和执行。

```sh
cd ~/Downloads
mkdir -m 755 /Users/Shared/ZMacRDS-M0
tar -xzf ZMacRDS-M0-arm64.tar.gz -C /Users/Shared/ZMacRDS-M0
```

从源码运行时，在已安装 Xcode Command Line Tools 的 Mac 上执行 `make m0-kit`，将 `build/` 下的压缩包复制到虚拟机。测试账号需要能在虚拟机的图形登录界面登录。请记录两个账号的短用户名、`id -u`、`sw_vers`、`uname -m` 和显示器配置，便于核对结果。

两个账号都应先完成首次登录向导并进入 Finder 桌面，再开始采样。测试中发现：首次登录向导尚未完成时，即使 `gui/UID` 已存在、单次身份检查显示已登录，LaunchAgent 也可能尚未产出样本。在每个测试账号的“系统设置 → 菜单栏”中显示“快速用户切换”，使用此菜单切换账号并保留原有登录会话；锁定屏幕本身不等于切换到另一个用户。

## 采样

1. 在账号 A 的桌面打开 Terminal，运行下面的启动命令。看到 `samples.jsonl` 的路径表示已收到第一条样本。
2. 用快速用户切换登录 B，保留 A 的登录会话。在 B 自己的 Terminal 运行同一条命令，两边使用相同的实验编号。
3. 在 B 的桌面停留至少 15 秒。切回 A，再停留至少 15 秒。每个代理约每 2 秒记录一次，最多 300 次，需在约 10 分钟的采样窗口内完成。
4. 在 A 运行停止命令，再切回 B，在 B 运行相同的停止命令。采样文件保存在各自的用户目录中。后续重测时，两边同时换一个新的实验编号。

启动命令

```sh
/Users/Shared/ZMacRDS-M0/m0 start vm01
```

停止命令

```sh
/Users/Shared/ZMacRDS-M0/m0 stop vm01
```

启动与停止命令均用普通测试账号执行。代理只在本次 GUI 登录会话中注册。M0 使用权限预检，录屏与辅助功能权限可以保持关闭。启动失败时查看该账号实验目录中的 `stderr.log` 及 Terminal 的错误输出。

## 核对

在管理员账号的 Terminal 中把两份日志复制到私有目录。将 `user_a` 和 `user_b` 替换为测试账号的实际短用户名，如修改过实验编号也需替换 `vm01`。

```sh
results=$(mktemp -d)
sudo cat "/Users/user_a/Library/Application Support/ZMacRDS/M0/vm01/samples.jsonl" > "$results/A.jsonl"
sudo cat "/Users/user_b/Library/Application Support/ZMacRDS/M0/vm01/samples.jsonl" > "$results/B.jsonl"
/Users/Shared/ZMacRDS-M0/m0 verify "$results/A.jsonl" "$results/B.jsonl"
```

输出 `PASS M0` 且退出码为 0 表示通过。请核对输出中的两个 UID 与实验账号是否对应，并保留两份原始日志和验收输出。验证器检查以下条件。

- 两份日志来自同一实验编号、同一次 macOS 启动。各自所有样本的身份稳定、用户已登录、进程 UID 等于 GUI UID。两边的 UID 和审计会话 ID 均不同。
- A 和 B 各在前台时，两边都持续观测到相同的前台 UID，两个采样区间至少重叠 4 秒。单边相邻样本间隔超过 5 秒即打断区间。Quartz 返回前后台标志时也须与前台 UID 相符。
- 屏幕录制与辅助功能预检结果随样本保留。输出中的 `granted` 计数表示预检返回允许的样本数。

若结果为 `FAIL M0`，保留日志和错误信息。如果启动提示没有收到样本，先检查该账号是否已完成首次登录向导，再检查 `stderr.log`；重试时使用新的实验编号。如果缺少某一方向的时间重叠，确认使用了快速用户切换、两个代理都已开始采样，再换新编号复测。如果报告 GUI UID 或审计会话异常，保留该虚拟机现场以定位原因。
