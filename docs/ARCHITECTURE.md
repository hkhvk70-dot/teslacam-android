# 模块与接口

## 数据路径

```text
车机 USB 写入
    ↓
USB 采集器／稳定文件视图（DeepSeek 部分，待合并）
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

采集层必须交付稳定可读的文件；网页层不能解决块设备的并发挂载、一致性和车机写入问题。请勿将读写同一文件系统的不同挂载点视为天然安全的共享机制。具体快照或同步实现须以采集器代码为准。

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
