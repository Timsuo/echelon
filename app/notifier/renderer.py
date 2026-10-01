from datetime import datetime
from zoneinfo import ZoneInfo

from app.history.renderer import coverage_warning
from app.llm.schemas import SummaryData


def render_summary(data: SummaryData, job: dict, count: int, timezone: str, gaps: list[dict] | None = None) -> str:
    zone = ZoneInfo(timezone)
    start = datetime.fromtimestamp(job["window_start"], zone).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(job["window_end"], zone).strftime("%Y-%m-%d %H:%M")
    lines = [f"【群聊总结 #{job['id']}】", f"群：{job['group_id']}",
             f"时间：{start} 至 {end}", f"消息：{count} 条", ""]
    if gaps:
        lines.extend([coverage_warning(gaps, timezone), ""])
    if count == 0:
        lines.append("此时间窗口没有已采集的消息。")
    for topic in data.topics:
        lines.extend([f"话题：{topic.title}", topic.summary])
        if topic.participants:
            lines.append("参与者：" + "、".join(topic.participants))
    for label, items in (("决定", data.decisions), ("待办", data.todos),
                         ("重要事件", data.important_events), ("不确定事项", data.uncertainties)):
        if items:
            lines.append(f"\n{label}：")
            lines.extend(f"- {item}" for item in items)
    if data.notable_message_ids:
        lines.append("\n参考消息 ID：" + "、".join(data.notable_message_ids))
    lines.append("\n仅基于本地已采集记录；总结可能存在误差。")
    return "\n".join(lines)
