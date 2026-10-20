# 模块与接口

## 控制面（2026-10-20 更新）

统一入口为 `controller/src/teslacam.sh`，安装后所有控制器模块位于同一 bin 目录。完整 start/stop/maintain 等操作持 lifecycle.lock；短 control.lock 保护状态转换，工作锁协调采集与映像检查。操作身份在 state/active_op.json，检查提交状态在 op_inprogress.json，二者分开。

services.json 分别定义服务命令、环境文件、日志与就绪判据。实例令牌/starttime/组与会话快照用于核验；status 输出 alive、ready、unmanaged 和完整性，不修改状态。reassert 同样先过生命周期/当前 boot 守卫，拒绝缺失、旧 boot、未知和未完成记录。

旧维护/恢复脚本仅提示退役。发布 pipeline 不启动网页/FRP/队列，`start` 才负责服务纳管。使用方法见 [USAGE](USAGE.md)。

## 数据路径

```text
车机 USB 写入
    ↓
USB 采集器（DeepSeek；只读映像视图不具备快照保证）
    ↓
events/TeslaCam/{RecentClips,SentryClips,SavedClips}
    ↓
pipeline/index.py → events/index.json
    ↓
web/serve.py ← web/index.html
    ├─ 历史事件预览：pipeline/transcode.sh → events/_proxy
    └─ 按需 15 秒分段预览 → live/
    ↓
Tailscale 私网或用户自行配置的转发 → 接收端浏览器
```

当前采集层只读挂载映像，按较新片段或跨扫描大小稳定来决定复制，用 `.part` 后重命名发布。该实现没有快照、写入冻结或主机提交协议；网页层不能补足此一致性保证。详见 [限制](COMPATIBILITY.md)。

`pipeline.sh` 每四轮调用一次全量 `collect.sh`，其余调用 `live_collect.sh`，然后生成索引。采集器共享挂载工作锁并检查 stop/hold。15 秒是循环休眠间隔，实际周期还包括采集与索引耗时，不代表 15 秒端到端直播延迟。

## 索引结构

路径相对于 `events`，均为示意数据：

```json
{
  "minutes": [{
    "key": "2025-01-01_12-00-00",
    "clips": {"front": "TeslaCam/RecentClips/2025-01-01_12-00-00-front.mp4"}
  }],
  "live": null,
  "events": []
}
```

实际 `clips` 可包含 `front`、`back`、`left_repeater`、`right_repeater`、`left_pillar`、`right_pillar`。四路总览要求前四路的有效输入。

事件对象使用 `SentryClips/<event>` 或 `SavedClips/<event>` 作为 `id`，包含 `kind`、`name`、`status`、`clip_map`、`proxies`、`mosaic`、`mosaic_map` 等字段。`clip_map` 为“分钟 → 镜头 → 相对路径”。索引器验证媒体元数据后原子替换索引；事件状态并不等于所有压缩预览已经生成。

## HTTP 接口

- `POST /`：使用 `k` 表单字段提交访问码，成功后写入登录 Cookie。
- `GET /index.json`：索引。
- `GET /api/live`：请求预览；参数包括 `view`、`minute`、可选事件 `event`、分段 `part` 及 `priority`。
- `GET /api/live?view=quad&cached=1`：只查已有缓存，不主动加入新编码任务。
- `GET /live-cache/<name>.mp4`：缓存片段，支持 Range。
- `GET /f/<relative-path>`：受鉴权和路径约束的文件读取。
- `GET /api/connection`：依据 HTTP socket 对端显示访问入口，返回 `access`、`peer_path`、`observed_at`，不返回凭据或设备地址。

`peer_path` 目前为 `unknown`。HTTP 私网地址不能提供底层 Tailscale direct/relay 状态。loopback 对端也可能是本机访问，因此 UI 使用“转发访问”，不会单凭它断言公网服务器。

## 编码与环境变量

HTTP 预览只启动一个编码工作线程，保留有限等待队列；历史压缩与 HTTP 预览共享 flock 编码锁。两个模块的锁路径必须一致。

| 模块 | 主要配置 |
|---|---|
| HTTP | `ROOT`, `PORT`, `TOKEN`, `LIVE_DIR`, `FFMPEG`, `FFPROBE`, `ENCODE_LOCK` |
| 索引 | `OUT`, `QUIET_MIN`, `FFPROBE` |
| 历史压缩 | `EV`, `PREFIX`, `FFMPEG`, `FFPROBE`, `TC_PYTHON`, `TC_ENCODE_LOCK` |

HTTP 的 `ROOT`、索引的 `OUT` 和压缩的 `EV` 应指向同一个事件根目录。将 `web/index.html` 安装至这个目录时应保留相对 API 路径。页面没有写死用户的服务器或私网 IP。

## 可选后台队列

在 services.json 将 transcode_queue.enabled 设为 true 时，由控制器启动 `transcode_queue.sh` / Python worker。worker 从索引中选择安静的完整/部分事件，优先最新事件；不依赖旧 `.last_request`。失败按当前源版本退避重试，源变化后可重新处理，不永久依赖 `.failed`；stop/hold 时不调度新任务。

worker 只锁自身单实例文件，不重复获取编码锁；实际 FFmpeg 锁由 `transcode.sh` 持有并在每路之间释放。services.json 为 HTTP 和压缩 worker 配置相同编码锁，避免自锁。后台整事件队列默认关闭，前台 15 秒按需预览继续可用。

流水线与可选队列都可能更新索引。索引、审计和探测缓存使用各自任务独占的临时文件再原子替换，避免同名临时文件相互覆盖。这保证发布 JSON 的完整性，不保证不同任务观察的录像树处于同一时刻。
