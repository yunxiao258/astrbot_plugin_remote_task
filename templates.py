"""任务模板库：内置常用定时任务模板。

每个模板含名称/中文标题/说明/默认 cron/默认命令与参数说明；
命令 `task template create <名称> [k=v]` 用参数渲染出 cron 与命令后，
走现有定时任务创建流程（权限 + 调度注册）。
模板定义独立于此文件，不改变任何现有存储结构。
"""


class TaskTemplate:
    """一个任务模板"""

    def __init__(self, name, title, description, cron, command, params=None, defaults=None):
        self.name = name              # 英文名（命令中使用）
        self.title = title            # 中文标题
        self.description = description
        self.cron = cron              # 默认 cron 表达式（5 段）
        self.command = command        # 默认任务命令（{param} 占位符）
        self.params = dict(params or {})      # 参数名 -> 说明
        self.defaults = dict(defaults or {})  # 参数名 -> 默认值

    def render(self, values: dict) -> tuple[str, str, str]:
        """用参数渲染出 (cron, 命令, 错误)；缺必填参数或渲染失败时 cron/命令为 None"""
        merged = dict(self.defaults)
        for k, v in (values or {}).items():
            if v is not None and str(v).strip():
                merged[str(k).strip()] = str(v).strip()
        missing = [k for k in self.params if k not in merged]
        if missing:
            hint = "，".join(f"{k}={v}" for k, v in self.params.items())
            return None, None, f"❌ 缺少参数: {', '.join(missing)}\n模板参数: {hint}"
        try:
            command = self.command.format(**merged)
        except (KeyError, ValueError, IndexError) as e:
            return None, None, f"❌ 参数渲染失败: {e}"
        return self.cron, command, ""

    def to_lines(self) -> list[str]:
        """模板列表展示行"""
        return [
            f"{self.title}（{self.name}）",
            f"  {self.description}",
            f"  默认 cron: {self.cron}",
        ]

    def detail_lines(self) -> list[str]:
        """模板详情展示行"""
        lines = [
            f"📄 模板详情: {self.title}（{self.name}）",
            f"说明: {self.description}",
            f"默认 cron: {self.cron}",
            f"默认命令: {self.command}",
        ]
        if self.params:
            lines.append("参数:")
            lines += [f"  {k}: {v}" for k, v in self.params.items()]
        lines.append("创建: /任务 模板 创建 <名称> [参数k=v]")
        return lines


TEMPLATES: list[TaskTemplate] = [
    TaskTemplate(
        name="daily_backup",
        title="每日备份",
        description="每天凌晨 2 点备份指定目录，并清理 7 天前的旧备份",
        cron="0 2 * * *",
        command="备份目录 {backup_dir} 到 {backup_root}，完成后清理 7 天前的旧备份",
        params={"backup_dir": "待备份目录（必填）", "backup_root": "备份存放目录（默认 ./backup）"},
        defaults={"backup_root": "./backup"},
    ),
    TaskTemplate(
        name="weekly_report",
        title="每周统计日报",
        description="每周一上午 9 点汇总上周统计数据并生成日报",
        cron="0 9 * * 1",
        command="汇总{week_scope}的统计数据，生成一份中文日报并列出关键指标变化",
        params={"week_scope": "统计范围说明（默认：最近 7 天）"},
        defaults={"week_scope": "最近 7 天"},
    ),
    TaskTemplate(
        name="temp_clean",
        title="定时清理临时文件",
        description="每天凌晨 3:30 清理临时目录中 3 天前的文件",
        cron="30 3 * * *",
        command="清理临时目录 {temp_dir} 中 3 天前的文件，并报告释放的空间",
        params={"temp_dir": "临时目录路径（必填）"},
    ),
    TaskTemplate(
        name="daily_news",
        title="每日新闻推送",
        description="每天早上 8 点抓取今日科技新闻要点并生成摘要",
        cron="0 8 * * *",
        command="抓取今日科技新闻要点，整理成 {news_count} 条摘要并推送",
        params={"news_count": "新闻条数（默认 5）"},
        defaults={"news_count": "5"},
    ),
    TaskTemplate(
        name="health_check",
        title="定时健康检查",
        description="每 30 分钟检查一次系统服务健康状态并报告异常",
        cron="*/30 * * * *",
        command="检查本机系统服务健康状态（CPU/内存/磁盘/关键进程），报告异常项",
    ),
    TaskTemplate(
        name="weekly_summary",
        title="每周报告生成",
        description="每周五上午 10 点生成本周工作报告（汇总 commit 与变更）",
        cron="0 10 * * 5",
        command="生成本周工作报告：汇总项目 {project_dir} 本周的 commit 与文件变更，输出 Markdown 报告",
        params={"project_dir": "项目目录（必填）"},
    ),
]


def find_template(name: str) -> TaskTemplate | None:
    """按英文名或中文标题查找模板；找不到返回 None"""
    if not name:
        return None
    key = name.strip().lower()
    title = name.strip()
    for tpl in TEMPLATES:
        if tpl.name.lower() == key or tpl.title == title or tpl.title.lower() == key:
            return tpl
    return None
