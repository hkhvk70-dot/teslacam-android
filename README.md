# TeslaCam Android

使用安卓手机接收特斯拉行车记录仪和哨兵录像，通过手机网页远程查看。项目由用户在实车上验证，USB 采集部分由 DeepSeek 协助开发，网页、压缩播放及 Tailscale 集成由 Codex 协助开发。

**当前为私有仓库实验整合版。网页与播放服务为 4.2，已合并 DeepSeek 交付的 USB 采集源码和教程。它依赖已获 Root 的兼容手机，不是独立 APK，也不是通用一键安装产品。此次合并尚未部署回手机或完成新的实车验收。**

## 已有功能

- 事件列表、镜头切换，以及前后左右四路压缩总览。
- 将已完成的原车录像按 15 秒分段压缩，缓存并预取下一段，供 Safari 等浏览器播放。
- 完成的历史事件可生成单路或四路合成预览。
- 访问码鉴权、HTTP Range、索引与路径校验。
- 手机和观看设备通过 Tailscale 私网连接；也可使用自行配置的服务器转发。
- 网页显示私网、转发或局域网入口。**私网入口不等于已确认 P2P**；当前无法从官方安卓客户端自动取得实时直连／中继状态。

“准实时”指浏览手机已经取得并处理完成的录像文件，不是车机摄像头直播。原车文件完成、同步及压缩都会带来延迟。Tailscale 能否直连取决于两端当前网络。

## 当前目录

```text
web/                  HTTP 服务与网页
pipeline/             录像索引和历史事件压缩
usb/                  机型相关的 USB、采集、健康检查及维护脚本
config/               不含凭据的配置示例
tests/                使用合成数据的回归测试
docs/ARCHITECTURE.md  模块关系和数据约定
docs/USB.md           USB 原理、部署前提与运行步骤
docs/INTEGRATION.md   两边交付的合并说明
docs/COMPATIBILITY.md 验证范围、存储一致性与已知限制
docs/TAILSCALE.md     私网访问和直连核验说明
docs/STATUS.md        验证范围及发布前待办
```

USB 方案复用手机厂商 USB HAL 和内核 mass-storage gadget。既有环境为小米 17 Pro Max／Android 16／HyperOS 3.0.319.0，以及中国大陆使用的 2025 款 Model 3 高性能版。其他机型和 ROM 未验证。Root 获取、个人恢复命令、真实运行配置和存储映像不随仓库分发。

**存储边界：**车机写入映像时，采集器只读挂载该映像再复制文件。这避免了第二个文件系统写入者，但不提供原子快照；主机缓存和同时写入仍可能造成不一致读取。文件大小稳定和视频校验不能证明端到端绝对可靠。断电、整夜运行和维护恢复仍待实车验证，不能保证持续录像绝不间断。详见 [兼容性说明](docs/COMPATIBILITY.md)。

## 开发验证

后端使用 Python 标准库；媒体处理依赖 FFmpeg／ffprobe。实际手机脚本还依赖 Android／Termux 路径及 shell 工具。Windows 上可以运行不依赖真实录像的测试，但不能据此验证 USB gadget 或实车行为。

```sh
python -B -m unittest discover -s tests -p 'test_*.py'
node tests/test_teslacam_ui_v41.cjs
node tests/test_teslacam_connection_ui_v42.cjs
```

HTTP 用例会短暂使用本机回环端口。测试不会连接车辆、手机或互联网。USB 脚本还做了 shell 语法检查，这不等于执行过挂载或实车测试。已满足 Root、权限、空白存储和机型条件的部署者可阅读 [USB 教程](docs/USB.md)。

## 运行约定

现有环境使用 `events/TeslaCam` 保存可读录像树，`events/index.json` 保存索引，`events/index.html` 为网页。HTTP 服务默认端口为 8081；通过环境变量读取路径和访问码。发布副本要求启动时设置非空 `TOKEN`，不提供默认通用密码。请勿提交实际访问码、录像、设备配置或登录资料。

`pipeline.sh` 默认关闭整事件预转码队列与 FRP；按需分段预览不依赖这两项。需要时在配置中显式启用。索引并发写入已改为每个任务独占临时文件，编码继续共用一把锁。

本仓库不分发磁盘映像、固件、Tailscale/FFmpeg 安装包或提权工具。第三方来源见 [依赖说明](docs/THIRD_PARTY.md)；项目整体许可由仓库所有者在公开前确认。
