# astrbot_plugin_remote_task

AstrBot 插件：远程任务助手，在群里下发任务给 opencode 执行，自动选择 serve API 或本地 run，实时推送进度，支持取消与历史查询。

## 功能

- 两种执行模式（自动选择）：
  - **serve API**：调用 `opencode serve`（HTTP API），适合常驻后台持续执行
  - **本地 run**：`opencode run "<描述>" --format json` 子进程方式，超时/权限卡住可终止
- 群内实时推送任务进度：开始 / 工具调用 / 完成 / 失败
- 多平台去重（双平台 / 多连接收到同一 `message_id` 只处理一次）
- 机器人自身回传消息不处理（防递归）
- 结果自动摘要并截断推送（`result_max_chars`）
- 权限卡住时提示放行（配合 `astrbot_plugin_status_sync` 的 serve 实时批准可在群内回复「同意 <ID>」）
- 任务持久化到 `data/tasks.json`，重启后保留历史并自动把未完成任务标记为失败
- `/任务 serve` 命令可直接托管 opencode serve 后台进程

## 使用方法

| 命令 | 说明 |
| --- | --- |
| `/任务 <描述>` | 下发任务（自动探测并选择执行模式） |
| `/任务列表` | 最近任务状态列表（紧凑形式） |
| `/任务详情 <id>` | 查看某个任务详情与结果 |
| `/任务取消 <id>` | 取消运行中的任务（仅管理员） |
| `/任务模式` | 查看当前执行模式（serve / 本地 run）与探测结果 |
| `/任务 serve 状态` | serve 进程状态与可用性探测 |
| `/任务 serve 启动` | 托管启动 opencode serve（仅管理员） |
| `/任务 serve 停止` | 停止 serve（仅管理员） |

下发 / 取消 / serve 管理仅限 `admin_umos` 白名单；未配置白名单时管理命令全部拒绝。

## 插件配置

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 插件总开关 |
| `serve_url` | opencode serve 服务地址（默认 `http://127.0.0.1:4096`） |
| `serve_port` | 插件托管 serve 的监听端口（默认 4096） |
| `serve_host` | 托管 serve 的监听主机（本机 `127.0.0.1`） |
| `serve_username` / `serve_password` | serve HTTP Basic Auth（可选；密码支持 `env:`/`file:`/`dpapi:`/`base64:` 前缀） |
| `serve_listen` | 是否订阅 serve 全局事件做进度推送与完成检测 |
| `opencode_exe` | opencode 可执行文件路径（留空自动从 PATH 查找） |
| `run_work_dir` | 本地 run 模式的工作目录（必填，不存在时拒绝下发） |
| `run_auto_approve` | 本地 run 是否自动批准未显式拒绝的权限（严格模式建议 `false`） |
| `run_no_output_timeout_seconds` | 本地 run 无新输出超时（秒，默认 120） |
| `serve_idle_complete_seconds` | serve 会话空闲判完成阈值（秒，默认 60） |
| `admin_umos` | 管理员会话 UMO 白名单（逗号分隔，必须配置） |
| `result_max_chars` | 结果推送最大字符数（默认 500） |
| `max_tasks` | 历史任务保留条数（默认 50） |
| `progress_push` | 是否推送进度关键节点 |
| `broadcast_umo` | 结果播报目标会话（留空播给下发会话） |

## 权限联动

任务执行中若 opencode 需要权限（`ask`），插件会广播提示。配合
[astrbot_plugin_status_sync](https://github.com/yunxiao258/astrbot_plugin_status_sync)
的 serve 实时批准：插件从 opencode 日志 `asking id=...` 行捕获权限 ID，
群内回复「同意 <ID>」即可现场放行，任务自动继续。

## 依赖

- Python 3.10+ / AstrBot 4.x
- `requests`（一般已装）
- 本地 run 模式需要 `opencode` 在 PATH（或配置 `opencode_exe`）