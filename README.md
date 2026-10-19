# TeslaCam Android

使用安卓手机接收特斯拉行车记录仪和哨兵录像，通过手机网页远程查看。项目由用户在实车上验证，USB 采集部分由 DeepSeek 协助开发，网页、压缩播放及 Tailscale 集成由 Codex 协助开发。

**当前为私有仓库整理版。网页与播放服务为 4.2；USB 采集源码和完整安装教程尚待合并。现有内容不是可直接安装的完整产品，也不是独立 APK。**

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
tests/                使用合成数据的回归测试
docs/ARCHITECTURE.md  模块关系和数据约定
docs/INTEGRATION.md   DeepSeek 交付与集成事项
docs/TAILSCALE.md     私网访问和直连核验说明
docs/STATUS.md        验证范围及发布前待办
```

USB 采集器、启动管理和针对用户设备的运行配置尚未包含。项目在已有提权环境的小米 17 Pro Max／Android 16／HyperOS 3.0.319.0 上工作过；这不能证明其他设备可复现，也不能宣传为免 Root。采集实现和权限条件需要随采集器教程一并确认。

## 开发验证

后端使用 Python 标准库；媒体处理依赖 FFmpeg／ffprobe。实际手机脚本还依赖 Android／Termux 路径及 shell 工具。Windows 上可以运行不依赖真实录像的测试，但不能据此验证 USB gadget 或实车行为。

```sh
python -B -m unittest discover -s tests -p 'test_*.py'
node tests/test_teslacam_ui_v41.cjs
node tests/test_teslacam_connection_ui_v42.cjs
```

HTTP 用例会短暂使用本机回环端口。测试不会连接车辆、手机或互联网。完整安装及启动命令将在采集部分合并后提供。

## 运行约定

现有环境使用 `events/TeslaCam` 保存可读录像树，`events/index.json` 保存索引，`events/index.html` 为网页。HTTP 服务默认端口为 8081；通过环境变量读取路径和访问码。发布副本要求启动时设置非空 `TOKEN`，不提供默认通用密码。请勿提交实际访问码、录像、设备配置或登录资料。

本仓库暂不分发磁盘映像、固件、Tailscale/FFmpeg 安装包或提权工具。第三方组件和采集源码的许可清单尚待补齐；项目整体许可由仓库所有者在公开前确认。
