# TeslaCam Android

用兼容的安卓手机接收特斯拉行车记录仪、哨兵录像，在另一台手机的浏览器里远程查看。手机同时承担录像存储、采集、索引、压缩和网页服务；通过 Tailscale 访问时，不需要购买自己的服务器。

**2026-10-20 更新：统一管理 USB 与后台服务，加入生命周期协调、进程身份核验、精细状态和恢复保护。网页/播放基线仍为 4.2。这是需要已有 Root 权限的实验项目，不是免 Root APK 或通用一键安装器。2026-10-21 起仓库公开可见。**

## 先看这些

| 想了解什么 | 文档 |
|---|---|
| 本次更新了什么 | [更新记录](CHANGELOG.md) |
| 怎么配置、启动、停止和恢复 | [使用说明](docs/USAGE.md)、[USB 前提与安装映射](docs/USB.md) |
| 我们踩过哪些坑 | [踩坑记录](docs/LESSONS_LEARNED.md) |
| 到底验证到了哪一步 | [验证范围与结果](docs/VALIDATION-20261020.md) |
| 旧版本如何迁移 | [升级说明](docs/UPGRADING.md) |
| 不买服务器怎么远程访问 | [Tailscale](docs/TAILSCALE.md) |

## 可以做什么

- 浏览事件、切换镜头，播放单路或前后左右四路压缩总览。
- 为已采集的原车片段按需生成 15 秒预览，缓存并预取下一段；支持 Safari、HTTP Range 和访问码登录。
- 通过统一入口管理采集、网页、看门狗及可选转码/FRP，避免 stop 后网页仍运行。
- 用持久操作记录和生命周期锁协调 start、stop、maintain，阻止停机过程中插入启动。
- 核验进程实例后才处理它；报告未纳管实例，不因名字相似就终止进程。
- 区分进程存活 `alive` 和服务就绪 `ready`，显示扫描完整性与拒绝原因。
- 看门狗在操作未完成、记录缺失、身份未知或跨重启时拒绝擅自重新导出。

“准实时”是查看已经录完、采集和处理完成的文件，**不是车机摄像头直播**。Tailscale 私网访问也不等于始终 P2P；网页目前不能自动核验底层直连/中继。

## 目录

```text
controller/src/      九个控制器模块 + 统一 shell 入口，安装至同一 bin 目录
controller/tests/    生命周期、身份竞态、状态、恢复保护等隔离测试
controller/run_tests.py  主机子集或 Android 原生回归入口
usb/                 USB、采集、看门狗、检查与已停用的旧入口
pipeline/            采集/索引循环、索引器、压缩和可选队列
web/                 HTTP 服务与网页
config/              无凭据配置示例
tests/               网页、媒体服务、索引与发布整合测试
docs/                使用、升级、设计、验证与踩坑说明
```

## 运行概要

已满足权限、兼容性、独立 FAT32 映像和安装前提后，在手机已有 Root 会话中：

```sh
sh /data/teslacam/bin/teslacam.sh status
# 仅在已准备好启动且没有正在运行的旧实例时：
sh /data/teslacam/bin/teslacam.sh start
```

**start/stop 会改变 USB 导出与后台服务，maintain 可能修改录像映像。不要把它们当成查询命令，也不要把 start 当作重复执行的健康检查。** 详细步骤见 [使用说明](docs/USAGE.md)。旧 `maint.sh`、`recover.sh`、`self_recover.sh` 只提示迁移并退出，不再执行恢复或提权。

默认使用 `/data/teslacam` 和 8081。访问码由自己的本机文件提供，没有通用默认密码；仓库不包含真实设备配置、录像、映像、账号资料或 Root 获取工具。

## 已验证与限制

指定环境：小米 17 Pro Max、Android 16 / HyperOS 3.0.319.0，中国大陆的 2025 款 Model 3 高性能版。此前已有人车实际录像和 Safari 远程播放记录。

这轮已完成 Android 原生 128 项控制器回归、私有更新/回滚夹具、辅助作业锁继承与常驻服务不继承锁验证，以及**手机连接电脑时**安全弹出后的实际 USB 拔插观察。九个控制器模块与修复后手机文件摘要一致。

本次仓库整合进一步收窄了流水线的启动职责，保留原仓库的新版事件队列并增加停止屏障；这些发布适配没有在本轮上传手机。完整仓库不应被描述成已经逐文件完成实车验收。

尚未验证实际掉电/重启、重启后 Root 可用性、实车重新插拔、数月无人值守。车机写映像时手机只读挂载，**仍然没有稳定快照或主机提交协议**，不能承诺绝对一致或持续录像永不中断。数据保留策略可能删除采集副本，部署前必须理解阈值。

## 开发验证

在仓库根目录：

```sh
python -B -m unittest discover -s tests -p 'test_*.py'
node tests/test_teslacam_ui_v41.cjs
node tests/test_teslacam_connection_ui_v42.cjs
python -B controller/run_tests.py
```

最后一条为主机子集；Windows 的字节锁替身不证明 Linux flock。Android/Termux 上可单独运行 `python -B controller/run_tests.py --native`，会在私有临时目录启动有限测试子进程，不操作业务 USB 或录像映像。详见 [控制器测试说明](controller/tests/README.md)。

运行依赖及来源见 [THIRD_PARTY](docs/THIRD_PARTY.md)。仓库已公开可见，整体许可证仍待所有者确定，本次未添加开源许可证。
