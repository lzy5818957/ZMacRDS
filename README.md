# ZMacRDS

用 Windows 自带的远程桌面客户端（`mstsc.exe`）连接一台 Mac，让不同用户同时操作各自的 macOS 图形桌面。这是一个研究项目，32 个并发用户是容量目标。

**当前进度**　M0 会话身份工具已实现，待双账号虚拟机验收。Windows RDP 连接和 32 人容量测试属于后续阶段。

## 从这里开始

按[双账号测试操作单](docs/M0_TEST.md)运行第一阶段验收。执行 `make m0-kit` 可生成包含代理和验证器的虚拟机测试包。[开发计划](docs/DEVELOPMENT_PLAN.md)列出了后续桌面隔离和 Windows 连接的验收条件。

若只需查看当前 Terminal 的会话身份，可在装有 Xcode Command Line Tools 的 Mac 上运行以下命令。

```sh
make probe
./build/session-probe
```

工具输出一行 JSON，记录当前进程的 UID、审计会话 ID、Quartz 图形会话和权限预检状态。请在第二个测试账号自己的 Terminal 中重复运行。两个账号输出不同 UID 只能证明取样来自不同账号，画面和输入隔离仍需按计划实测。工具不会创建账号、监听网络、截图或发送键鼠事件。

项目原创代码使用 [MIT 许可证](LICENSE)。
