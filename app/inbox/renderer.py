from datetime import datetime
from zoneinfo import ZoneInfo


def timestamp(value: int | None, timezone: str) -> str:
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M") if value else "未知"


def render_inbox(items: list[dict], unread: int, timezone: str) -> str:
    lines = ["【Echelon Inbox】", f"未读：{unread}"]
    for item in items:
        lines.extend(["", f"#{item['id']} [{item['status'].upper()}]", item["title"],
                      f"群：{item['source_group_id'] or '未知'}", f"时间：{timestamp(item['event_time'], timezone)}"])
        if item.get('priority'):
            lines.append(f"{item['priority'].upper()} · {(item.get('category') or '未分类').upper()}")
        if item.get('coverage_status') == 'warning':
            lines.append('⚠ 存在未确认采集缺口')
    if not items:
        lines.append("暂无条目。")
    lines.extend(["", "使用：/detail <id>、/archive <id>"])
    return "\n".join(lines)


def render_detail(item: dict, timezone: str) -> str:
    lines = [f"【Echelon #{item['id']}】", f"标题：{item['title']}", f"状态：{item['status'].upper()}",
             f"来源群：{item['source_group_id'] or '未知'}", f"时间：{timestamp(item['event_time'], timezone)}",
             "", "内容：", item["summary"] or "暂无摘要", f"来源消息：{item['message_count']} 条", "", "附件："]
    facts = [f"优先级：{(item.get('priority') or '未分类').upper()}",
             f"分类：{(item.get('category') or '未分类').upper()}",
             '标签：' + (' '.join('#' + label for label in item.get('labels', [])) or '无'),
             '截止：' + (timestamp(item['deadline_at'], timezone) if item.get('deadline_at') else item.get('deadline_text') or '未明确'),
             '需要行动：' + (item.get('action_text') or '否'),
             f"判断置信度：{item.get('confidence') if item.get('confidence') is not None else '未分类'}",
             '理由：' + (item.get('reason') or '未分类')]
    if item.get('coverage_status') == 'warning':
        facts.append('⚠ 该事项来自存在未确认采集缺口的时间窗口，可能缺少上下文。')
    elif item.get('triaged_at'):
        facts.append('Coverage：未发现已知未确认缺口；不保证绝对完整。')
    lines[3:3] = facts
    for index, attachment in enumerate(item["attachments"], 1):
        size = f"{attachment['file_size'] / 1024:.1f} KB" if attachment["file_size"] is not None else "大小未知"
        lines.extend([f"{index}. {attachment['filename']}", f"   {size} · {attachment['download_status']}"])
    if not item["attachments"]:
        lines.append("无")
    lines.extend(["", f"使用：/file {item['id']} <附件序号>、/archive {item['id']}"])
    return "\n".join(lines)
