# USB 实现与运行教程

这是针对已满足权限和机型前提的实验项目说明。**本次整合未部署手机；不要在正在工作的手机上重建映像、重配 USB 或覆盖脚本。**

## 原理与前提

车机是 USB 主机；手机复用厂商 HAL 管理的 gadget `g1`，用内核 `usb_f_mass_storage` 将 FAT32 映像作为可写 LUN 提供给车机。不是普通 OTG 读 U 盘，也没有 FunctionFS 用户态实现。

交付方报告的已验证环境：小米 17 Pro Max，Android 16／HyperOS 3.0.319.0；HAL 支持 `mass_storage,adb`。`g1` 与控制器 `a600000.dwc3` 是该设备的观察值。脚本没有通用控制器选择，换机型不能只改一个 UDC_NAME 就假定兼容。

前置条件：

- 设备所有者已经获得 Root，能操作 configfs、USB 属性和 loop 挂载。Shizuku 不能替代这些权限。
- 所有者已按映像实际 SELinux 标签配置必要权限。仓库不获取 Root、不关闭 SELinux，也不提供提权/恢复工具。
- Termux 环境有 Python、FFmpeg／ffprobe、flock、timeout、nice 等工具；系统有适配的挂载、loop 和 FAT 工具。
- 已单独准备空白、容量合适的 FAT32 映像，含根目录 `TeslaCam`，并确认实际存储空间足够。不要在已有录像的映像上练习格式化。
- 使用与车机录像数据口匹配并支持数据传输的线材。接头是 A 还是 C，不能独自证明数据能力。

Root、介质初始化和厂商组合需要单独确认，仓库没有这些步骤的一键安装器。优先用正常 Termux 用户和官方包源安装依赖，不照搬旧环境的忽略依赖、递归改属主或未知重打包安装包。

## 文件安装映射

由设备所有者先备份已有脚本和配置，再逐项审核安装。以下不是自动覆盖当前手机的命令：

| 仓库文件 | 运行位置 |
|---|---|
| `usb/*.sh` | `/data/teslacam/bin/` 下同名文件 |
| `pipeline/*.sh`、`pipeline/*.py` | `/data/teslacam/bin/` 下同名文件 |
| `web/serve.py` | `/data/teslacam/bin/serve.py` |
| `web/index.html` | `/data/teslacam/events/index.html` |
| `config/msd.conf.example` | 审核修改后存为 `/data/teslacam/msd.conf` |

shell 使用 LF 换行，设置适当执行权限。脚本仍以 `/data/teslacam`、`g1` 和默认 Termux 路径为约定，尚未全面参数化。后台队列的 `.sh` 和 `.py` 两个文件缺一不可。

明确配置 `IMG`，确认它指向测试介质。保留天数、分钟数与空间阈值会触发删除副本，先读 [生命周期](FILE_MAP.md)。在设备本机生成自己的随机访问码，存为 `/data/teslacam/token` 并限制访问权限；不要把内容放进仓库、截图或 URL。HTTP 服务拒绝空访问码。

## 从已准备环境启动

仅在空白测试环境、车机尚未依赖手机录像时，由所有者手动操作：

1. 配好 `msd.conf`，默认关闭 FRP 和整事件预转码，确认备份及 Root 会话。
2. 用 `msd.sh status` 查看状态，确认目标后运行 `msd.sh up`；后者会改变 USB/LUN，不是只读检查。
3. 连接到车机录像口，确认车机识别并实际保存测试录像。
4. 只启动一份 `msdwatch.sh` 和一份 `pipeline.sh`。不要并行手动运行两个采集器，它们共用挂载点和状态文件。
5. 在 Root 会话中运行 `health.sh`，检查组合、LUN、UDC、采集与服务；它会写本机 health.json，不是纯只读操作。
6. 确认 `events/TeslaCam` 和索引有有效录像后，再按 [Tailscale 说明](TAILSCALE.md) 在接收端播放。

`pipeline.sh` 管理循环采集、索引和 8081 服务。默认不使用个人服务器；自建 FRP 时自行提供配置，显式设 `ENABLE_FRP=1`。需要完整事件预转码时设 `ENABLE_EVENT_QUEUE=1`，前台 15 秒按需压缩不依赖它。

## 停止与重启

停止前先在车机结束录像，确认重要内容已复制。先停止对应项目的看门狗、流水线和可选队列，再执行 `msd.sh down`。**只执行 down 不是完整停机**，看门狗可能重新配置 USB。应核对项目进程，不能全局杀死所有 Python 或 FFmpeg。

安全移除手机、插回原 U 盘后，核对车机恢复录像。`down` 只是解除手机 LUN 并切回 ADB，不会把手机变成另一只普通 U 盘。

包内没有私人 `r`、recover 或提权入口，不应要求新部署者输入不存在的 `r`。临时 Root 重启后不能假定仍有效；所有者重新满足权限前提后再恢复项目，自动恢复不在本版范围。

## 维护与一致性

日常采集器不主动摘 LUN，但看门狗恢复和维护可能改变 USB 状态。只读挂载并不是快照，文件大小稳定也不是跨系统提交协议。

`maint.sh` / `fsck_img.sh` 保留作实验维护参考，没有加入自动启动。idle、充电状态或时间戳都不能单独证明安全卸载。维护前应停止车机写入、协调停止采集和看门狗、备份数据，并在专用测试介质验证；强制修复和摘盘会中断录像。

本版采集路径只实现 FAT32。不要在车机格式化后假定仍兼容：车机格式化通常建立 exFAT，而本脚本未验证 exFAT。

来源：[Tesla 录像存储要求](https://www.tesla.cn/ownersmanual/model3/zh_cn/GUID-F311BBCA-2532-4D04-B88C-DBA784ADEE21.html)、[Linux backing-file 限制](https://docs.kernel.org/usb/mass-storage.html)。
