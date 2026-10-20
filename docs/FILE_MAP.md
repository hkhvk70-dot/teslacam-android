# 文件用途与生命周期

| 文件 | 用途和副作用 |
|---|---|
| `controller/src/teslacam.sh`、`*.py` | 生命周期、持久状态、导出门禁、服务纳管、身份和只读状态；start/stop/maintain 等有副作用 |
| `usb/msd.sh` | Root USB up/down/status；up/down 改 USB 属性或 LUN，可能应用所有者预置策略 |
| `usb/msdwatch.sh` | 每 5 秒检查组合/LUN/UDC，异常时请求控制器 reassert；不直接写 configfs |
| `usb/collect.sh` | 只读映像后复制三类录像；按时间和空间压力删除采集副本 |
| `usb/live_collect.sh` | 复制较早 RecentClips；与全量采集共享工作锁和停止屏障，不另行手动并发启动 |
| `usb/health.sh` | 状态诊断并写 health.json；需 Root，不证明媒体一致性 |
| `usb/maint.sh`、`recover.sh`、`self_recover.sh` | 已退役，只输出迁移说明并退出 78，不调用其他命令 |
| `usb/fsck_img.sh` | 控制器使用的 FAT 检查/修复辅助作业，可能修改映像；不手动在线运行 |
| `pipeline/pipeline.sh` | 仅循环采集/索引，要求 control_managed；不自行启动其他服务 |
| `pipeline/index.py` | 校验媒体并发布 index/audit/cache，每任务独占临时文件 |
| `pipeline/transcode.sh` | 带锁、超时和验证的单事件压缩，不修改原片 |
| `pipeline/transcode_queue.sh`、`.py` | 可选队列，单实例、安静事件、源版本退避、索引刷新 |
| `web/serve.py` | 鉴权、文件读取、按需编码和预览缓存清理 |
| `web/index.html` | 4.2 网页、选择播放、预取和入口提示 |

采集产物位于 `events/TeslaCam`，历史压缩位于 `events/_proxy`，分段预览位于 `live`。这些运行数据不入库。

事件 `complete`/`partial`/`growing` 不代表所有预览已生成。4.2 可以请求未预压事件的按需片段，不会依旧交接文字必然回落巨大原片。

队列不再用 `.last_request`，也不把旧 `.failed` 当永久禁止标记；`.done` 仍由 guarded 转码脚本管理。
