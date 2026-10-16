# 第三方依赖与分发边界

运行依赖：Python 3.12、qrcode 8.2、PySide6 / Qt / shiboken6 6.8.3、python-vlc 3.0.21203、NumPy 2.2.6、opencv-python-headless 4.11.0.86。验证探针使用 psutil 7.1.0；构建使用 PyInstaller 6.16.0。完整实际环境见 `build-manifest.json`、锁文件及 `third-party-licenses/`。

| 组件 | 许可概要 |
| --- | --- |
| qrcode | BSD-3-Clause；只用矩阵生成，不引入 Pillow |
| Python | PSF，运行时自带 notices |
| PySide6 / Qt / shiboken6 | 本构建按可用 LGPL/GPL 条款核对，主要绑定标注 LGPL-3.0；具体 Qt 模块和附带 FFmpeg 以收集的原文为准 |
| python-vlc | LGPL-2.1-or-later |
| NumPy | BSD-3-Clause，附带 OpenBLAS / LAPACK / GCC runtime 等许可见原文 |
| opencv-python-headless | Apache-2.0，内含组件见 LICENSE-3RD-PARTY |
| psutil | BSD-3-Clause |
| PyInstaller | GPL-2.0-or-later with bootloader exception |
| libVLC | 用户机器外部安装，不随包再分发；仍需遵守上游 VLC 条款 |

`packaging/collect_notices.py` 从已安装发行包收集原始许可文本，并附上 CPython 3.12.13 原始许可证，随候选包的 `docs/third-party-licenses/` 提供；实际构建记录位于包内 `docs/build-manifest.json`。生成文件只写入 `build/` staging，不回写源码 `docs/`。动态 Qt 库保留独立文件；项目源码和构建脚本以独立 source ZIP 附件交付，不嵌套在平台 ZIP 中，便于替换兼容库后重建。对外商业分发前仍需检查 LGPL 的依赖对应源码提供、替换/重链接及修改告知义务；项目 source ZIP 不等于已附齐所有依赖对应源码，本表也不是法律合规验收。

`assets/alarm.wav` 是本项目以正弦波合成的原创短音效，无采样素材，按 CC0 提供。业务源码的对外授权由项目所有者决定，本次不擅自为其指定开源许可证。
