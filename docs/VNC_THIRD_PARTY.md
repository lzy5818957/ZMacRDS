# VNC 实验包第三方组件

项目自己的代码采用仓库 LICENSE。打包的 Python 运行时及其依赖保留各自原有许可证，不应把整个组合标为全部 MIT。完整运行时目录按原样打包，包含 site-packages 中安装的源码、dist-info、许可证和原生库附带通知；版本锁定见 `tools/vnc-test-requirements.txt`。

| 组件 | 本轮版本 | 来源 / 安装元数据许可证 |
|---|---|---|
| CPython / python-build-standalone | 3.13.18 / 20261026 | [Astral 官方发行](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026)，运行时自带许可证 |
| asyncvnc | 1.3.0 | [barneygale/asyncvnc](https://github.com/barneygale/asyncvnc)，随包 GPL v3 文本 |
| asyncvnc2 | 2.0.0 | [andrews239/asyncvnc2](https://github.com/andrews239/asyncvnc2)，随包 GPL v3 文本 |
| async-timeout | 5.0.1 | [aio-libs/async-timeout](https://github.com/aio-libs/async-timeout)，Apache 2 |
| cryptography | 50.0.2 | [pyca/cryptography](https://github.com/pyca/cryptography)，Apache-2.0 OR BSD-3-Clause |
| NumPy | 2.5.4 | [numpy/numpy](https://github.com/numpy/numpy)，BSD-3-Clause 及所附第三方通知 |
| cffi | 2.1.1 | [python-cffi/cffi](https://github.com/python-cffi/cffi)，MIT-0 |
| keysymdef | 1.2.0 | [barneygale/keysymdef](https://github.com/barneygale/keysymdef)，安装元数据标为 X11 |
| pycparser | 3.12 | [eliben/pycparser](https://github.com/eliben/pycparser)，BSD-3-Clause |

该归档是内部有界测试包，尚不是对外发行。原始 CPython 发行归档 SHA-256 为 `8392b6ea51d94391befb2c31ebe75463f045d0b69dfcf5e1bb3234e1f5fa3405`。账号配置、私钥、原生后端 pin、桌面画面和测试文档均不放入测试包。TigerVNC 客户端从官方发行获取，不放入包内。

协议研究参考 macvnc / zDesk 的公开记录，具体固定版本和验证范围见回归报告；测试包不包含这两个项目的代码。
