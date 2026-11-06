# M1 · 双用户画面与输入实验

M0 已通过。M1 首先验证 ScreenCaptureKit 返回的像素是否确实属于本账号、持续刷新，然后验证整桌面和输入。单独窗口采集通过不等于 M1 通过，也不代表 RDP 可用。

## 构建与部署

```sh
make m1-kit
python3 -m unittest discover -s tests -v
```

将 `build/ZMacRDS-M1-arm64.tar.gz` 放进测试虚拟机，解压到新的临时目录，再从该目录执行 `sudo ./install-m1 /Users/Shared/ZMacRDS-M1`。更新前先结束所有测试账号的 M1 应用；安装器拒绝覆盖正在运行的实例，以新 inode 替换整个应用包并保留旧包。不要直接将 tar 解压到正在使用的应用上。

只在两个已完成首次登录的普通测试账号运行。不要在含个人资料的日常桌面做整桌面实验。应用以 ad-hoc 签名打包，修改二进制会改变签名要求。若开关已开启而预检仍未授权，在系统设置中移除该应用的旧权限条目，再用“+”添加当前安装的 `.app`，完成正常授权并换新实验编号。VM03 曾实际记录 TCC 的 `Failed to match existing code requirement`；不能把这种签名不匹配误判为后台采集不支持。macOS 27.2 的辅助功能权限显示在“Privacy & Security → Device Control and Data Access”。不修改 TCC 数据库。

## 只读窗口采集

在 A、B 各自桌面的 Terminal 使用同一个新编号：

```sh
/Users/Shared/ZMacRDS-M1/m1 start example-01 window 300
```

也可由 VM 管理员在既有图形会话中使用 `launchctl asuser UID sudo -H -u USER /Users/Shared/ZMacRDS-M1/m1 start ...` 调度；不是通过 SSH 创建图形会话。应用必须实际出现在该账号桌面。

测试窗口包含 UID、随机 nonce 和每秒递增计数器的条码。代理以 2 fps 为上限采集自己的窗口，从真实 BGRA 帧中解码条码并校验 CRC；不把绘制计数器当作采集计数器。不写截图或视频，不采集音频。macOS 14.4 及以后首先用仅本进程的内容枚举接口；旧系统可能要求录屏授权。

保持 A、B 分别在前台至少 30 秒，期间另一账号保持登录。代理自动在指定时间（30–3600 秒）结束，也可在相同账号调用：

```sh
/Users/Shared/ZMacRDS-M1/m1 stop example-01
```

证据保存在各账号 `~/Library/Application Support/ZMacRDS/M1/example-01/events.jsonl`，目录 0700、文件 0600。收集为 A、B 两份，在开发机运行：

```sh
python3 tools/m1-verify.py A.jsonl B.jsonl
```

验证器要求两个不同图形身份、同一系统启动和实验、有效的本账号像素标记，以及两种前台状态下持续刷新的共同区间。前台切换预留 5 秒过渡，每个方向至少再有 15 秒有效观测；新图像间隔不得超过 3.5 秒。返回 `PASS CAPTURE (window)` 只说明窗口采集门槛通过。

## 整桌面采集

先完成窗口采集，再使用新编号及 `desktop` 模式重复。按正常系统提示授予测试应用录屏权限，必要时重新启动并换新编号。工具不修改 TCC 数据库，不默认回退到控制台或其他窗口。

桌面模式选择包含完整测试窗口的显示器，捕获其桌面像素并查找相同条码。窗口需要保持无遮挡；分辨率固定，不改变显示缩放。单屏通过后还须补外接屏或无头配置。`PASS CAPTURE (desktop)` 仍不涵盖输入、注销、权限撤销和会话恢复。

## 有限输入实验

仅在只读采集通过后开启。用以下命令触发正常辅助功能授权提示，授权须在系统设置中完成：

```sh
/Users/Shared/ZMacRDS-M1/m1 accessibility example-01
```

将测试窗口设为该会话的焦点，然后在每个账号分别执行：

```sh
/Users/Shared/ZMacRDS-M1/m1 input example-01
```

每份实验仅允许一批 100 次试验，约 20 秒；每次发送字符键下/键上、鼠标按下/拖动/抬起共 5 个事件。事件通过 `kCGSessionEventTap` 投递，没有指定目标 PID。每个事件携带本次 nonce、试验序号和事件序号。日志记录发送和接收标签；手动输入不记录文本。失去辅助功能权限或本会话应用焦点时立即停止下一次试验。

先测试前台，再在两侧同时启动批次、保持一侧后台；用新编号反向重复。每份日志还需包含前面两种前台状态的只读采样。用 `python3 tools/m1-verify.py A.jsonl B.jsonl --input` 核对所有 500 个发送标签在对应用户恰好出现一次、另一用户零次，检查 key-down 接收文字、全部事件的投递类型、时延和前台 UID，以及鼠标接收坐标，以及两侧输入批次是否至少重叠 15 秒。此批次是 Unicode 文字投递探针，虚拟键码固定为 0。AppKit 在 key-up 可按该键码映射出 `a`，不一定保留 Unicode 覆盖字符串；因此文字核对仅用于 key-down，key-up 仍逐一核对标签、账号、类型和时延。完整物理键码、修饰键与输入法另测。Apple 对该行为的说明见 [CGEvent Unicode API](https://developer.apple.com/documentation/coregraphics/cgevent/keyboardsetunicodestring(stringlength:unicodestring:))。

`PASS INPUT BATCH` 只覆盖一组测试窗口输入。还须反向重复、扩展到另一个应用、真实文字输入、焦点切换、注销和撤权，才满足开发计划的 M1。

后台黑屏、刷新停止或跨账号输入均保存失败日志，停止真实桌面 RDP 集成，先分析限制。应用退出会结束采集与输入，不新增网络监听或开机任务。

## 跨应用编辑和中文输入

测试包包含独立进程 `ZMacRDS-M1-Editor.app`，使用普通 `NSTextView` 编辑合成测试文字。它记录实际文档、选择范围、输入法组合状态及收到的事件标签。代理不能直接改写该编辑器的文字，也不使用指定 PID 的输入投递。两侧各启动一个编辑器及一个桌面代理，窗口分别固定在右、左侧；测试显示器需要容纳两者（VM03 为 1920×1200）。

```sh
/Users/Shared/ZMacRDS-M1/m1 editor cross-01 3600
/Users/Shared/ZMacRDS-M1/m1 start cross-01 desktop 3600
```

将对应账号代理目录的 `state.json` 带到开发机，生成有界脚本，再送回同一测试账号：

```sh
python3 tools/m1-script.py A-state.json cross-01 unicode --count 100 > A-unicode.json
python3 tools/m1-script.py A-state.json cross-01 pinyin --count 3 > A-pinyin.json
# 在对应测试账号内提交；前一脚本完成后再提交下一份。
/Users/Shared/ZMacRDS-M1/m1 script cross-01 A-pinyin.json
/Users/Shared/ZMacRDS-M1/m1 script cross-01 A-unicode.json
```

代理只接受本账号私有目录中的完整请求，并核对 UID、审计会话、系统启动 ID、随机 nonce 和连接代次。该机制是本地实验控制，不是网络身份认证。请求只允许固定测试动作和已登记的编辑器；焦点移到第三个应用、身份变化或权限丢失时中止。

Unicode 批次逐次替换成 `用户UID：中文序号🌍`，用鼠标拖出非空选择，每十次在代理和编辑器之间切换焦点。拼音批次切至系统简体拼音，发送 `nihao`、空格、`shijie`、空格的物理键码，要求实际产生组合过程并提交“你好世界”。不能用直接 Unicode 写入替代拼音验收。首次启用拼音须同时启用其父输入法；VM03 上已打开的编辑器曾仍直接插入英文，重开编辑器后才正常组合，应保存失败日志再换新实验编号。脚本结束会恢复原输入源。

两侧同时完成一个前台方向的全部批次后，再通过快速用户切换反向重复。期间不要在测试账号打开设置或遮住代理条码。各账号的编辑器证据位于 `~/Library/Application Support/ZMacRDS/Editor/CASE/`，包括 `events.jsonl`、`state.json` 和合成 `document.txt`；不得用于个人文档。

## 本地断开与恢复

在完整批次后断开 A，保持至少 20 秒，B 继续采集和编辑。重放旧请求应被拒绝，再重连 A：

```sh
/Users/Shared/ZMacRDS-M1/m1 disconnect cross-01
# 至少 20 秒后
/Users/Shared/ZMacRDS-M1/m1 reconnect cross-01
```

重连后重新取 A 的 `state.json`，用 `m1-script.py ... resume --count 100` 生成恢复脚本。该脚本要求断开前最后一份文档为 Unicode 第 100 次结果；确认原编辑器 PID 和文字保留，再追加“，恢复成功”。旧 nonce / 代次请求不能重新取得控制权。

使用 `m1 stop RUN` 和 `m1 editor-stop CASE` 正常停止两个代理和编辑器后，将证据收集为 `A.jsonl`、`B.jsonl`、`A-editor.jsonl`、`B-editor.jsonl`：

```sh
python3 tools/m1-cross-verify.py RESULT_DIRECTORY --recovery
```

验证器检查两个前台方向各 100 次 Unicode、真实拼音组合、独立编辑器文档、编辑输入事件恰好投递一次且没有跨账号投递，并验证恢复间隔内 A 无输出、B 仍有新帧、旧请求被拒绝及原文档可继续编辑。代理视图按 AppKit 默认规则不接收用于激活窗口的首次点击；这类事件单独要求 2 秒内的成功焦点检查，不能放宽其他输入的接收检查，参见 [Apple 鼠标事件说明](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/EventOverview/HandlingMouseEvents/HandlingMouseEvents.html)。此处的恢复是本地代理连接恢复；RDP 客户端重连另验。注销后重新登录、撤销录屏权限也须另外记录，不能由此结果代替。

## 显示器变化诊断

`display-probe` 只读取当前进程所在图形会话的显示器编号、尺寸及在线状态，不截图、不切换账号、不修改显示器。可以在 A、B 的 GUI 域分别调用，记录前后台变化后的编号；编号不同本身不证明桌面隔离可用。

VM03 初版整桌面流在快速用户切换时返回 `SCStreamErrorNoCaptureSource`（-3815）。当前应用对此特定错误最多重试三次：等一秒后，在原来的普通用户 GUI 进程中重新枚举，只接受该进程自己的测试窗口，再查找包含它的显示器并建立新流。`capture_interrupted` 和 `capture_target` 日志记录中断、重试次数及实际显示器编号。权限失败和身份变化不会被该逻辑掩盖，也不会改用其他账号的内容。是否恢复成功仍由捕获像素和双向连续观测验证；单纯重试成功回调不算通过。
