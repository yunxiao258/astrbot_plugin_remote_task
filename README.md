# astrbot_plugin_remote_task

AstrBot 插件：远程任务助手，在群里下发任务给 opencode 执行，自动选择 serve API 或本地 run，实时推送进度，支持取消、历史查询、定时任务与任务模板库。

版本：v1.2.1 | 许可证：MIT

## 功能

- 两种执行模式（自动选择）：
  - **serve API**：调用 `opencode serve`（HTTP API），适合常驻后台持续执行
  - **本地 run**：`opencode run "<描述>" --format json` 子进程方式，超时/权限卡住可终止
- 群内实时推送任务进度：开始 / 工具调用 / 完成 / 失败（同类型事件限流去重，防刷屏）
- 多平台去重（双平台 / 多连接收到同一 `message_id` 只处理一次）；机器人自身回传消息不处理（防递归）
- 结果自动摘要并截断推送（`result_max_chars`），疑似含错误时附带 ⚠️ 高亮
- serve 模式权限实时审批：任务卡在权限请求时用 `/任务 同意 <id>` / `/任务 拒绝 <id>` 现场放行或拒绝（支持 always 记住）
- 任务持久化到 `data/tasks.json`，重启后保留历史并自动把未完成任务标记为失败；超限任务自动归档（`data/archive.json`）
- 失败自动重试：`retry_max` 次、间隔 `retry_interval_seconds` 秒（已取消/已完成不重试）
- **定时任务**：`/任务 定时 <cron> <描述>` 按标准 5 段 cron 表达式创建定时任务，注册到 AstrBot 调度器，定义持久化到 `data/schedule.json`
- **任务日历**：`/任务 日历 [N]` 查看未来 N 天（默认 7，上限 30）各定时任务的触发排期
- **任务模板库**：内置 6 个常用定时任务模板，`/任务 模板 创建 <名称> [k=v]` 一键创建定时任务
- **结果回调推送**：任务完成 / 失败 / 异常时把结果摘要推送到指定群或用户（`--callback` 参数或 `callback_target` 配置）
- `/任务 serve` 命令可直接托管 opencode serve 后台进程（PID 记录在 `data/serve.json`，日志 `data/serve.log`）

## 使用方法

| 命令 | 说明 |
| --- | --- |
| `/任务 <描述> [--callback 目标]` | 下发任务（自动探测并选择执行模式） |
| `/任务列表` | 最近任务状态列表（紧凑形式） |
| `/任务详情 <id>` | 查看某个任务详情与结果 |
| `/任务取消 <id>` | 取消运行中的任务（仅管理员） |
| `/任务模式` | 查看当前执行模式（serve / 本地 run）与探测结果 |
| `/任务 serve 状态` | serve 进程状态与可用性探测 |
| `/任务 serve 启动` | 托管启动 opencode serve（仅管理员） |
| `/任务 serve 停止` | 停止 serve（仅管理员） |
| `/任务同意 <id> [always]` | 放行任务挂起的权限请求（always 表示会话内始终允许该工具） |
| `/任务拒绝 <id>` | 拒绝权限请求（别名 `/任务放行`） |
| `/任务下发 <描述> [--callback 目标]` | 强制按本地 run 模式下发（别名 `/任务执行`、`/任务跑`） |
| `/任务 定时 <cron> <描述>` | 按 cron 表达式创建定时任务（仅管理员） |
| `/任务 定时列表` | 查看定时任务 |
| `/任务 定时删除 <id>` | 删除定时任务（仅管理员） |
| `/任务 模板 列表` | 列出全部内置任务模板 |
| `/任务 模板 显示 <名称>` | 查看模板详情（说明 / cron / 命令 / 参数） |
| `/任务 模板 创建 <名称> [k=v]` | 用模板创建定时任务（仅管理员，缺必填参数会提示） |
| `/任务 日历 [天数]` | 查看未来 N 天定时任务触发排期（默认 7 天，上限 30） |
| `/任务 归档 [id]` | 查看归档任务列表（省略 id 时列出最近归档） |

> 命令均支持英文别名：`/task`、`/任务列表` 等；模板子命令可用英文（`template list` 等），日历可用 `calendar`。
> 下发 / 取消 / serve 管理 / 定时 / 模板创建 / 日历 / 归档仅限 `admin_umos` 白名单；未配置白名单时管理命令全部拒绝（查询类命令不受限）。

### 定时任务 cron 表达式

标准 5 段格式：`分 时 日 月 周`（分 0-59、时 0-23、日 1-31、月 1-12、周 0-7，0 与 7 均为周日）。

支持 `*`（任意）、`1,2,3`（枚举）、`1-5`（区间）、`*/15`（步进）、`1-30/5`（区间内步进）、`5/15`（从 5 起每 15）等写法；日 / 周双限定时采用标准 cron 的 OR 语义（任一命中即触发）。

示例：`/任务 定时 0 9 * * * 每天早上汇总更新`（每天 09:00 触发）。

### 结果回调推送（--callback）

下发任务时附带 `--callback` 参数，任务完成 / 失败 / 执行异常时会把结果摘要推送到指定目标：

| 写法 | 说明 |
| --- | --- |
| `--callback @123` | 推送到会话所在平台的群 123（`platform:GroupMessage:123`） |
| `--callback u@456` | 推送到会话所在平台的用户 456（`platform:PrivateMessage:456`） |
| `--callback default:GroupMessage:9` | 完整 UMO 透传，可同时写多个（英文逗号分隔） |

- 例：`/任务 检查磁盘 --callback @123`
- 任务级 `--callback` 优先于配置；也可在配置 `callback_target` 中设置全局回调目标（此时所有任务结果都会推送，无需逐任务指定）
- 推送内容包含任务 ID、完成/失败状态、退出码（本地 run）或执行模式（serve）与结果摘要

### 任务模板库

内置 6 个模板（`templates.py`）：

| 名称（英文名） | 默认 cron | 说明 | 参数 |
| --- | --- | --- | --- |
| 每日备份（`daily_backup`） | `0 2 * * *` | 每天凌晨 2 点备份指定目录，并清理 7 天前的旧备份 | `backup_dir`（必填）、`backup_root`（默认 `./backup`） |
| 每周统计日报（`weekly_report`） | `0 9 * * 1` | 每周一上午 9 点汇总上周统计数据并生成日报 | `week_scope`（默认「最近 7 天」） |
| 定时清理临时文件（`temp_clean`） | `30 3 * * *` | 每天凌晨 3:30 清理临时目录中 3 天前的文件 | `temp_dir`（必填） |
| 每日新闻推送（`daily_news`） | `0 8 * * *` | 每天早上 8 点抓取今日科技新闻要点并生成摘要 | `news_count`（默认 5） |
| 定时健康检查（`health_check`） | `*/30 * * * *` | 每 30 分钟检查一次系统服务健康状态并报告异常 | 无 |
| 每周报告生成（`weekly_summary`） | `0 10 * * 5` | 每周五上午 10 点生成本周工作报告（汇总 commit 与变更） | `project_dir`（必填） |

例：`/任务 模板 创建 每日备份 backup_dir=D:\data`（渲染参数后走定时任务创建流程，支持按英文名或中文标题查找模板）。

## 插件配置

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `serve_url` | `http://127.0.0.1:4096` | opencode serve 服务地址 |
| `serve_port` | `4096` | 插件托管 serve 的监听端口 |
| `serve_host` | `127.0.0.1` | 托管 serve 的监听主机（本机） |
| `serve_username` / `serve_password` | 空 | serve HTTP Basic Auth（可选；密码支持 `env:`/`file:`/`dpapi:`/`base64:` 前缀） |
| `serve_listen` | `true` | 是否订阅 serve 全局事件做进度推送与完成检测 |
| `opencode_exe` | 空 | opencode 可执行文件路径（留空自动从 PATH 查找） |
| `run_work_dir` | 空 | 本地 run 模式的工作目录（必填，不存在时拒绝下发） |
| `run_auto_approve` | `false` | 本地 run 是否自动批准未显式拒绝的权限（严格模式建议 `false`） |
| `run_permission_allow` | 空 | 本地 run 权限规则（JSON，如 `{"bash":"allow"}`），注入 opencode 内联配置，避免 headless 模式权限挂起（代码读取，未在 `_conf_schema.json` 中声明） |
| `run_no_output_timeout_seconds` | `120` | 本地 run 无新输出超时（秒，最小 30），超时视为卡住并终止 |
| `serve_idle_complete_seconds` | `60` | serve 会话空闲判完成阈值（秒，最小 30） |
| `admin_umos` | 空 | 管理员会话 UMO 白名单（逗号分隔，必须配置，否则管理命令全部拒绝） |
| `result_max_chars` | `500` | 结果推送最大字符数 |
| `max_tasks` | `50` | `tasks.json` 历史任务保留条数（超出自动归档） |
| `archive_max` | `200` | 归档文件保留的最大任务条数（超出丢弃最旧） |
| `progress_push` | `true` | 是否推送进度关键节点 |
| `notify_mention_creator` | `true` | 任务完成/失败播报时 @ 下发者（onebot 群聊生效） |
| `broadcast_umo` | 空 | 进度/结果播报目标会话（留空播给下发会话） |
| `callback_target` | 空 | 结果回调推送目标：UMO 或 `@群ID` / `u@用户ID`（沿用下发会话平台），逗号分隔；留空则仅当任务提交时带 `--callback` 才推送 |
| `retry_max` | `0` | 任务失败后自动重试的最大次数（0 为不重试） |
| `retry_interval_seconds` | `60` | 自动重试之间的等待间隔（秒） |

## 权限联动

- serve 模式任务执行中若 opencode 需要权限（`ask`），插件会广播提示，管理员在群内回复 `/任务 同意 <id>`（或 `/任务 拒绝 <id>`）即可现场放行 / 拒绝，任务自动继续；`always` 表示会话内始终允许该工具
- 本地 run 模式无法动态审批：可在配置 `run_permission_allow` 中放行规则（或开启 `run_auto_approve` 全部放行），否则权限卡住时按 `run_no_output_timeout_seconds` 超时终止并提示
- 配合 [astrbot_plugin_status_sync](https://github.com/yunxiao258/astrbot_plugin_status_sync) 的 serve 实时批准，可在群内回复「同意 <ID>」放行

## 数据存储

插件数据保存在 `data/` 目录下：

| 文件 | 内容 |
| --- | --- |
| `tasks.json` | 任务历史（最近 `max_tasks` 条） |
| `archive.json` | 被裁剪任务的归档（最近 `archive_max` 条） |
| `schedule.json` | 定时任务定义（cron 表达式 + 描述 + 发起人） |
| `serve.json` | 托管 serve 进程的 PID / 端口 / 日志路径 |
| `serve.log` | 托管 serve 进程输出日志 |

## 依赖

- Python 3.10+ / AstrBot 4.x
- `requests`（一般已装）
- 本地 run 模式需要 `opencode` 在 PATH（或配置 `opencode_exe`）

## 更新记录

### v1.2.1

- 新增任务模板库（`templates.py`）：内置 6 个常用模板，`/任务 模板 列表 / 显示 / 创建`
- 新增结果回调推送：`--callback` 参数与 `callback_target` 配置，支持 `@群ID` / `u@用户ID` / 完整 UMO
- 新增任务日历：`/任务 日历 [N]` 展示未来 N 天定时任务触发排期（标准 5 段 cron 解析器，`cron.py`）
- 新增定时任务（`/任务 定时 <cron> <描述>`、`定时列表`、`定时删除`），定义持久化到 `schedule.json`
- 新增失败自动重试（`retry_max` / `retry_interval_seconds`）与任务归档（`/任务 归档`）
- 新增完成播报 @ 下发者（`notify_mention_creator`）与结果错误关键词 ⚠️ 高亮