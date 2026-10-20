# 使用说明

适用于已有 Root、兼容 USB gadget、Termux 与独立录像映像的实验环境。首次安装前读 [USB 前提](USB.md)，旧版本读 [迁移](UPGRADING.md)。本项目不包含 Root 获取或刷机步骤。

## 一、配置

所有运行文件汇总到 `/data/teslacam/bin`。核对 Python、ffmpeg/ffprobe、flock、timeout、挂载与 FAT 工具路径，保留 LF 换行。配置示例不等于适配所有设备。

1. 按自己的介质配置 `msd.conf`，明确 `IMG`。不要在已有录像上格式化。
2. 审核 `config/services.json.example` 后保存为 `/data/teslacam/services.json`。控制器读取的是顶层服务名字典，不是额外包一层 `services`。
3. 在本机私下设置 `/data/teslacam/token`，使用非空随机访问码并限制文件权限。服务从 `env_files.TOKEN` 读取；不要把实际值写到 JSON、命令历史、URL 或仓库。
4. `serve.ready.host` 必须匹配真实监听地址。本仓库网页为双栈 IPv6 listener，示例用 `::1`；只监听 IPv4 的移植版需要相应调整。
5. 默认只启用 pipeline、serve、msdwatch。整事件队列和 FRP 通过各自 `enabled` 显式启用；旧 `ENABLE_FRP` / `ENABLE_EVENT_QUEUE` 不再承担服务启停。

网页与历史转码必须共用同一个编码锁。服务就绪为 alive 的条目只证明进程活着，不能证明业务健康。

## 二、先查状态

以下在手机已有 Root 会话执行：

```sh
sh /data/teslacam/bin/teslacam.sh status
```

`status` 不写业务状态、不读取输出访问码、不杀进程；它是非原子观察，`ok: true` 表示查询完成，**不等于所有服务健康**。重点看：

| 字段 | 怎么看 |
|---|---|
| `lun` | exported / detached / unknown |
| `permission_reasons` | 为什么被阻止、需要检查或恢复 |
| `active_op` | 操作是否 finished，退出码及 recovery_required |
| `services.<名称>.identity` | verified 才表示实例身份确认 |
| `alive` / `ready` | 进程在不在 / 当前就绪判据是否满足，null 不等于 false |
| `unmanaged` | 匹配项目特征但身份未纳管，先查来源，不盲杀 |
| `inspection.complete` | 观察不完整就不能凭缺失列表判安全 |

## 三、启动、观看

确认没有运行中的旧实例、当前状态允许启动，车端已结束对目标存储的写入并完成迁移准备后：

```sh
sh /data/teslacam/bin/teslacam.sh start
sh /data/teslacam/bin/teslacam.sh status
```

start 可能执行映像检查、导出 USB 并启动服务。**不要定时循环 start，也不要在正常运行时为了“确认一下”再 start**；重复启动会被拒绝，失败操作可能留下需要处理的记录。

接入车机录像数据口，实际录制一个测试事件，确认车机能回放、手机索引有该事件，再从浏览器播放。请保留可用的普通录像盘作为回退介质。

Tailscale 两端登录同一网络并连接，退出节点保持不使用，浏览器打开：

```text
http://<车内安卓手机的 Tailscale IPv4>:8081/
```

输入本机配置的录像访问码。局域网可使用手机的局域网地址；公网转发地址由自己的 FRP/代理配置决定，仓库不提供通用公网地址。网页能显示入口类型，直连/中继需按 [Tailscale 文档](TAILSCALE.md) 在播放期间独立核验。

新设备要求输入的是录像网页访问码，不是 Google 或 Tailscale 密码。忘记时只在设备本机私下查看自己的 token 文件，或经协调停止后设置新的值并重新启动服务；服务启动时读取 TOKEN，单改文件不会自动更换运行中服务的值。

## 四、停止与维护

先让车端结束录像，保存重要片段，确认可以中断录像，再运行：

```sh
sh /data/teslacam/bin/teslacam.sh stop
sh /data/teslacam/bin/teslacam.sh status
```

stop 会撤销导出，并按登记身份停止整个服务组；必须核对 LUN 已 detached、服务确实退出、无未知残留。单独 `msd.sh down` 不等于完整停止。涉及 USB 切换时优先使用已经建立的 Wi-Fi ADB，USB ADB 可能因重枚举掉线。

仅在已停止、LUN 脱离、重要数据备份完成且确有检查需要时：

```sh
sh /data/teslacam/bin/teslacam.sh maintain
```

maintain 可能写入映像；检查成功后仍需显式 start 才恢复录像。它不是在线扫描按钮。不要在导出期间手工调用 fsck 或对运行映像做主机修复。

## 五、拒绝时如何处理

- `lifecycle_busy`：可能还有检查后代持锁，等它结束；不要删除锁文件来“解锁”。锁文件存在不等于被持有。
- `active_op_incomplete` / `recovery_required`：保留记录和日志，先查失败原因。明确 stop 可受控收尾；只有成功 stop 才解除对应恢复限制。
- `boot_changed` / `active_op_missing`：看门狗不会自行恢复。确认权限和状态后走控制器显式恢复流程，不能伪造 boot_id 或删除保护记录。
- `unmanaged` / `identity_unknown`：核对是不是旧手动启动进程、权限不足或不完整扫描；不能凭 argv 或数字 PID 发信号。
- `BUSY` / `UNKNOWN`：不自动清理。`clean-orphans --confirm` 需要停止意图、LUN 脱离和工作锁内复核，属于有副作用的高级操作。
- `clear-hold` 需要明确原因和前置校验，不能当作日常重试按钮，也不能靠它绕过 incomplete 或 dirty。

不要通过删 `active_op.json`、保护标记、锁文件，或全局杀 Python/FFmpeg 来制造健康状态。失败时保留证据优先。

## 六、长期放车里

保持供电、温控、空间余量与所需后台权限；Tailscale、移动网络和 Android 后台保活分别观察。Android 同一用户通常只有一个活动 VPN，CarPlay 类 VPN 与 Tailscale 的切换不在本次实现范围内。

当前没有新安装开机自动恢复，重启后 Root 是否可用仍未验证。定期确认车机真正在录像、网页能播放新事件，而不只是端口可连。几个月无人值守尚无验证依据。
