# ZMacRDS

用 Windows 自带的远程桌面客户端（`mstsc.exe`）连接一台 Mac，让不同用户同时操作各自的 macOS 图形桌面。这是一个研究项目，32 个并发用户是容量目标。

**当前进度**　已提供开发计划和本机会话诊断工具。目前没有可供 Windows 连接的 RDP 服务端，也没有双用户或 32 用户测试结果。

## 从这里开始

1. 阅读[开发计划](docs/DEVELOPMENT_PLAN.md)，了解双账号实验和各阶段验收条件。
2. 在装有 Xcode Command Line Tools 的 Mac 上，从一个已登录测试账号的 Terminal 运行以下命令。

```sh
make probe
./build/session-probe
```

工具输出一行 JSON，记录当前进程的 UID、审计会话 ID、Quartz 图形会话和权限预检状态。请在第二个测试账号自己的 Terminal 中重复运行。两个账号输出不同 UID 只能证明取样来自不同账号，画面和输入隔离仍需按计划实测。工具不会创建账号、监听网络、截图或发送键鼠事件。

项目原创代码使用 [MIT 许可证](LICENSE)。
