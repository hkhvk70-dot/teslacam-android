# 来源与运行时依赖

USB 源码和既有运行记录来自 DeepSeek 安全交付包，摘要和取舍见 [合并说明](INTEGRATION.md)。交付方声明没有复用 TeslaUSB、FunctionFS 用户态实现或其他 Android App 源码；USB/SCSI 由手机现有内核和厂商 HAL 提供。这不替代完整来源审计。

网页、HTTP、索引修复、guarded 转码和整合测试由用户在 Codex 协助下开发。仓库没有依赖二进制、内核或固件。

| 组件 | 来源和用途 | 当前处理 |
|---|---|---|
| mass-storage / libcomposite | 手机内核；USB 块存储 | 不分发内核/模块，依赖 ROM |
| USB HAL / 系统工具 | 手机 ROM；组合与基础命令 | 不分发，机型兼容需核对 |
| Python | Termux 对应包；HTTP/索引/队列 | 脚本只用标准库 |
| FFmpeg / ffprobe | Termux 对应包；媒体处理 | 具体二进制许可与构建相关，不随仓库分发 |
| flock / timeout / nice | Termux/系统；互斥/时限/优先级 | 须检查实际安装 |
| Tailscale | 官方客户端；私网访问 | 用户自行安装和登录 |
| frp / nginx | 用户可选服务 | FRP 默认关闭，无私人配置 |
| Root/SELinux 工具 | 所有者已有环境 | 运行前提，不获取/分发 |

Shizuku 可能存在于原环境，但不能凭普通 ADB 权限替代本方案的 Root/configfs 权限。

项目整体许可仍由仓库所有者选择。私有整合不代表已经完成公开许可审核，也不授权随意打包运行依赖。
