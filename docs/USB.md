# USB 前提与安装映射

本页配合 [日常使用](USAGE.md)。这是针对兼容手机的实验部署说明，不是操作正在工作的手机的自动脚本。

车机作为 USB 主机，手机通过厂商 HAL 管理的 mass-storage gadget 把 FAT32 文件映像作为可写 LUN 导出。手机从只读 loop 视图复制文件。不是普通 OTG，也不能用普通扩展坞或双头 U 盘让两个主机同时共享块存储。

## 前提

- 设备所有者已有 Root 及必要 configfs/loop/SELinux 权限；Shizuku/普通 ADB 不能替代。
- 指定环境为小米 17 Pro Max、Android 16 / HyperOS 3.0.319.0，存在 `g1` 及 `mass_storage,adb` 支持。路径与 USB 控制器均为机型约定。
- Termux 提供 Python、FFmpeg/ffprobe、flock、timeout、nice；系统工具与 FAT 工具需可用。不要假设 Root shell 的 PATH 含 Termux。
- 所有者独立准备含 `TeslaCam` 根目录的空白 FAT32 映像，核对容量和可用空间。仓库不提供格式化生产录像映像的命令。
- 数据线和车机接口需真正支持录像数据。本实现不支持任意 exFAT/GPT，车机重新格式化后的布局必须重新核对。

## 文件映射

由所有者备份并逐项审核；复制文件不代表可以跳过运行状态协调。

| 仓库 | 手机运行位置 |
|---|---|
| `controller/src/*.py`、`teslacam.sh` | `/data/teslacam/bin/` 同名文件，九模块须成套 |
| `usb/*.sh` | `/data/teslacam/bin/` 同名文件；三个旧入口只提示退役 |
| `pipeline/*.sh`、`*.py` | `/data/teslacam/bin/` 同名文件 |
| `web/serve.py` | `/data/teslacam/bin/serve.py` |
| `web/index.html` | `/data/teslacam/events/index.html` |
| `config/msd.conf.example` | 审核后 `/data/teslacam/msd.conf` |
| `config/services.json.example` | 审核后 `/data/teslacam/services.json` |

使用 LF 换行、正确所有者/权限和 SELinux 标签；不要通过全局改权限或关闭 SELinux 消除错误。配置与访问码不能随代码提交。

`IMG` 必须在控制器、msd.conf 与采集器之间一致；默认 `/data/teslacam/cam_disk.img`。配置例子中最近录像保留分钟数、事件天数和空间阈值会触发删除采集副本，必须先理解 [文件生命周期](FILE_MAP.md)。

只使用统一入口启停，不另行后台启动 pipeline、watchdog 或 serve。新装好后先 status，再在可中断环境 start，最后实际验收车机录像和浏览器播放。迁移旧运行实例时遵循 [升级说明](UPGRADING.md)。

## 一致性边界

共享工作锁协调采集/维护，生命周期锁协调启停，不等于把车机的写入变成原子快照。只读 loop、大小稳定和 MP4 元数据校验不证明目录/FAT/内容始终同步。不能承诺持续录像绝不中断。

生产录像映像没有在本轮被 fsck 修复。Windows 的 D 盘提示按所有者决定不处理，不提供绕过决定的自动修复步骤。掉电、实车重连和长期运行仍需独立验证。
