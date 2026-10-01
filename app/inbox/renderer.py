from datetime import datetime
from zoneinfo import ZoneInfo


def timestamp(value: int | None, timezone: str) -> str:
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M") if value else "未知"


def render_inbox(items: list[dict], unread: int, timezone: str) -> str:
    lines = ["【Echelon Inbox】", f"未读：{unread}"]
    for item in items:
        lines.extend(["", f"#{item['id']} [{item['status'].upper()}]", item["title"],
                      f"群：{item['source_group_id'] or '未知'}", f"时间：{timestamp(item['event_time'], timezone)}"])
    if not items:
        lines.append("暂无条目。")
    lines.extend(["", "使用：/detail <id>、/archive <id>"])
    return "\n".join(lines)


def render_detail(item: dict, timezone: str) -> str:
    lines = [f"【Echelon #{item['id']}】", f"标题：{item['title']}", f"状态：{item['status'].upper()}",
             f"来源群：{item['source_group_id'] or '未知'}", f"时间：{timestamp(item['event_time'], timezone)}",
             "", "内容：", item["summary"] or "暂无摘要", f"来源消息：{item['message_count']} 条", "", "附件："]
    for index, attachment in enumerate(item["attachments"], 1):
        size = f"{attachment['file_size'] / 1024:.1f} KB" if attachment["file_size"] is not None else "大小未知"
        lines.extend([f"{index}. {attachment['filename']}", f"   {size} · {attachment['download_status']}"])
    if not item["attachments"]:
        lines.append("无")
    lines.extend(["", f"使用：/file {item['id']} <附件序号>、/archive {item['id']}"])
    return "\n".join(lines)
