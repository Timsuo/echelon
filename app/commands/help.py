from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    description: str
    examples: tuple[str, ...]
    category: str
    aliases: tuple[str, ...] = ()


COMMANDS = (
    CommandSpec("status", "/status", "服务运行状态",
        "检查 OneBot 连接、数据库消息、附件、Inbox、DeepSeek、任务和运行时间。\n"
        "/status 不检查历史覆盖完整性，历史覆盖请使用 /coverage。无修改副作用。", ("/status",), "状态与可靠性"),
    CommandSpec("coverage", "/coverage", "消息时间线覆盖情况",
        "显示连接、最后实时接收、最近断连、历史补偿、周期核验和未确认缺口。\n"
        "LIKELY_COVERED：历史范围跨过缺口两端，但无法保证绝对完整。\n"
        "PARTIAL：只能覆盖部分范围，或有无效记录。\nUNKNOWN：空结果或无法判断。\nFAILED：回查失败。\n"
        "PENDING / RUNNING：等待恢复或恢复中。只读查询，不发起同步。", ("/coverage",), "状态与可靠性"),
    CommandSpec("sync", "/sync [群号]", "有限历史核验",
        "不带群号：为白名单中非 IGNORE 群建立后台核验任务；带群号：仅该群。\n"
        "按 history.periodic_count 拉取最近有限条消息（最多500），可能补入消息、附件和 Inbox。\n"
        "不是下载整个群历史记录，只是 bounded best-effort consistency check。\n"
        "任务持久保存，完成后私聊结果；重复排队不会无限累积相同活动任务。", ("/sync", "/sync 756155087"), "状态与可靠性"),
    CommandSpec("inbox", "/inbox [unread|high|critical|deadline|action]", "收件箱列表",
        "/inbox：最近的非归档条目，默认10条；/inbox unread：仅未读。列表不会标记已读。\n"
        "high / critical：对应优先级；deadline：有截止文字或日期；action：需要行动。筛选不组合。\n"
        "/detail <id> 会将未读标为 read；/archive <id> 只归档，不删除源消息和附件。", ("/inbox", "/inbox unread"), "收件箱"),
    CommandSpec("detail", "/detail <id>", "查看条目及附件序号",
        "id 是 /inbox 展示的条目编号。显示来源、内容、消息数量、附件、分类、优先级、标签、行动、截止、置信度和覆盖提示。\n"
        "副作用：unread → read；已归档条目保持 archived，不增加 revision。", ("/detail 12",), "收件箱"),
    CommandSpec("archive", "/archive <id>", "归档条目",
        "只将条目状态改为 archived，默认 /inbox 不再显示。\n源消息、附件和磁盘文件都不会删除。", ("/archive 12",), "收件箱"),
    CommandSpec("file", "/file <inbox_id> <attachment_index>", "获取附件",
        "12 是 InboxItem ID，1 是 /detail 展示的附件序号（从1开始）。\n"
        "仅转发 downloaded 且通过账号、路径和哈希验证的附件给 ADMIN_QQ。\n"
        "不能输入文件路径；尚未完成或超限不会强行下载。发送任务进入持久 outbox。", ("/file 12 1",), "收件箱"),
    CommandSpec("summary", "/summary <时间窗口>", "按时间窗口总结群消息",
        "30m：最近30分钟；2h：最近2小时；today：配置时区今天00:00至当前。\n"
        "支持 m/h 窗口，最多7天。仅白名单且 Summary=ON、Mode != IGNORE 的群参与。\n"
        "创建后台任务，相关聊天文本发送给 DeepSeek；无消息时不调用模型。\n"
        "窗口存在未确认采集缺口时，Python 添加 Coverage Warning，不阻止生成。", ("/summary 30m", "/summary 2h", "/summary today"), "总结"),
    CommandSpec("groups", "/groups", "查看群策略列表",
        "SUMMARY_ONLY：保存消息、可总结、不进入 Inbox，通常不下载附件。\n"
        "INBOX：保存消息、可总结，批量 LLM 聚合消息，文件可进入 Inbox 并自动下载。\n"
        "PRIORITY：类似 INBOX，历史回查更频繁、分类等待更短；Phase 4 才有实时优先级提醒。\n"
        "IGNORE：停止业务采集及历史核验。\n"
        "如果只是不想收到提醒但仍希望以后总结，不要用 ignore，应使用 summary_only。\n"
        "上述是模式默认值，独立开关可以覆盖。此命令只读。", ("/groups",), "群策略"),
    CommandSpec("group", "/group <群号>", "查看单群策略",
        "显示本账号白名单群的 alias、mode 和独立开关。\n这是群号，不是 Inbox ID。模式解释见 /help groups。", ("/group 756155087",), "群策略"),
    CommandSpec("config", "/config <群号或别名> <配置意图>", "生成群策略变更提案",
        "只配置白名单内群。第一个独立数字 token 才是显式群号；正文日期、天数、MB 数字不是群号。\n"
        "别名必须唯一；同名或无匹配时请指定群号。LLM 不能生成目标群号。\n"
        "明确 mode/alias/开关命令在本地解析；其他自然语言后台发送给 DeepSeek。\n"
        "/config → 解析 → 显示 Before → After → /confirm <id> 才生效。\n"
        "取消：/cancel <id>。提案10分钟过期，重复确认幂等；配置已变化时需重新提交。\n"
        "切换 mode 会展开模式默认开关；单独关闭下载只修改下载字段。请仔细核对 diff。\n"
        "/config 不能将新群加入 allowlist，请手动修改 config.yaml 并重启。\n"
        "个人重要性偏好请用 /pref；与群策略共用 /confirm 和 /cancel。\n"
        "当前不支持自动到期策略或每群 MB 阈值；遇到这些要求请使用现有明确开关，不会实现定时优先级提醒。",
        ("/config 756155087 mode inbox", "/config 高数群只需要总结", "/config 高数群不要自动下载文件",
         "/config 班级群重点关注", "/config 756155087 alias 高数群"), "群策略"),
    CommandSpec("prefs", "/prefs", "查看个人分类偏好",
        "展示本账号的重要/低优先级关键词、重要发送者和分类偏好。只读。\n"
        "偏好参与后续 LLM 分类，不自动重算旧 Inbox，也不启用即时通知。", ("/prefs",), "个人偏好"),
    CommandSpec("pref", "/pref <自然语言>", "生成个人偏好提案",
        "后台解析允许的偏好增删，显示 Before → After，10分钟内 /confirm <id> 才生效；/cancel <id> 取消。\n"
        "关键词每类最多30个、每个40字，发送者最多30个，分类使用固定枚举。\n"
        "只改明确提出的偏好；不能修改群号、白名单、文件路径、通知计划或 quiet hours。\n"
        "请求文本发送给 DeepSeek，解析失败不修改偏好。请检查提案差异。",
        ("/pref 考试和调课对我很重要", "/pref 123456789 是老师，他的消息需要重点关注",
         "/pref 讲座通常是低优先级", "/pref 课程资料正常优先级即可"), "个人偏好"),
    CommandSpec("confirm", "/confirm <提案ID>", "应用群策略或个人偏好提案",
        "只接受本管理员、本机器人账号、未过期的 pending 提案。\n应用显示的差异；重复确认不会重复修改。", ("/confirm 42",), "群策略"),
    CommandSpec("cancel", "/cancel <提案ID>", "取消配置提案",
        "取消 pending 提案，不修改群配置。不用于撤销已经确认的变更；撤销请创建新提案。", ("/cancel 42",), "群策略"),
    CommandSpec("help", "/help [命令]", "查看帮助",
        "不带参数显示分类首页；带命令名显示参数、行为、副作用和例子。\n所有命令仅限 ADMIN_QQ；普通私聊不触发命令。", ("/help", "/help config"), "帮助"),
)


def render_help(argument: str = "") -> str:
    name = argument.removeprefix("/")
    if name:
        spec = next((item for item in COMMANDS if name == item.name or name in item.aliases), None)
        if spec is None:
            return "未知命令。\n\n发送 /help 查看 Echelon 支持的命令。"
        return f"【/{spec.name}】\n{spec.summary}\n\n用法：{spec.usage}\n\n{spec.description}\n\n示例：\n" + "\n".join(spec.examples)
    lines = ["【Echelon 帮助】", "仅 ADMIN_QQ 可执行命令。"]
    for category in dict.fromkeys(spec.category for spec in COMMANDS if spec.name != "help"):
        lines.extend(["", category, "  ".join("/" + spec.name for spec in COMMANDS if spec.category == category)])
    lines.extend(["", "查看详细说明：/help <命令>", "例如：/help summary、/help config、/help inbox、/help coverage"])
    return "\n".join(lines)
