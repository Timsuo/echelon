from datetime import datetime
from zoneinfo import ZoneInfo


def stamp(value: float | None, timezone: str) -> str:
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S") if value is not None else "尚未记录"


def coverage_warning(gaps: list[dict], timezone: str) -> str:
    if not gaps:
        return ""
    lines = ["⚠ 数据覆盖警告", "总结窗口内存在未确认完整的采集缺口："]
    for gap in gaps[:10]:
        lines.append(f"{stamp(gap['started_at'], timezone)} — {stamp(gap['ended_at'], timezone)} [{gap['coverage'].upper()}]")
    if len(gaps) > 10:
        lines.append(f"另有 {len(gaps) - 10} 个缺口，请查看 /coverage。")
    lines.append("当前摘要可能遗漏消息。NapCat/QQ 历史接口无法保证完整恢复。")
    return "\n".join(lines)


def recovery_report(gaps: list[dict], jobs: list[dict], timezone: str) -> str:
    states = {gap['recovery_status'] for gap in gaps}
    state = next((name for name in ("failed", "unknown", "partial") if name in states), "likely_covered")
    return ("【Echelon 采集恢复报告】\n"
        f"离线/断连时间：{stamp(min(g['started_at'] for g in gaps), timezone)} — "
        f"{stamp(max(g['ended_at'] for g in gaps), timezone)}\n"
        f"合并缺口：{len(gaps)}；原因：{', '.join(sorted({g['reason'] for g in gaps}))}\n"
        f"涉及群：{len({j['group_id'] for j in jobs})}\n历史回补：获取 {sum(j['messages_received'] for j in jobs)} 条，"
        f"新增 {sum(j['messages_inserted'] for j in jobs)} 条\n覆盖状态：{state.upper()}\n"
        + ("历史范围已覆盖缺口两端附近，但无法提供绝对完整性保证。" if state == "likely_covered" else
           "该时间段仍有未确认覆盖的部分，总结和 Inbox 可能存在遗漏。")
        + "\nNapCat/QQ 历史接口不是完整离线事件重放机制。\n查看：/coverage")
