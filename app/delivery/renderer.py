from datetime import datetime
from zoneinfo import ZoneInfo

from app.rendering.icons import CATEGORY_ICON, COVERAGE_WARNING, PRIORITY_ICON, plain


def stamp(value, timezone):
    return datetime.fromtimestamp(value, ZoneInfo(timezone)).strftime('%m-%d %H:%M') if value is not None else '未明确'


def render_alert(item, kind, previous, coverage, timezone, now, event_time):
    title = '【🔔 Echelon 重要提醒】'
    if kind == 'recovery':
        title = '【📥 离线期间回补提醒】'
    elif previous:
        title = '【🔄 优先级升级】' if previous['priority'] != 'critical' and item['priority'] == 'critical' else '【🔄 事项更新】'
    lines = [title, f"{PRIORITY_ICON[item['priority']]} {item['priority'].upper()} · #{item['id']}",
             f"{CATEGORY_ICON.get(item['category'], '💬')} {plain(item['title'])}", plain(item['summary'])]
    if kind == 'recovery':
        lines.extend([f'原消息时间：{stamp(event_time, timezone)}', f'恢复分类时间：{stamp(item["triaged_at"], timezone)}'])
    if previous and previous.get('deadline_at') != item['deadline_at']:
        lines.append(f'旧截止：{stamp(previous.get("deadline_at"), timezone)}')
    if item['deadline_at'] is not None or item['deadline_text']:
        lines.append('⏰ 截止：' + (stamp(item['deadline_at'], timezone) if item['deadline_at'] else plain(item['deadline_text'])))
    if item['action_required']:
        lines.append('📋 需要：' + plain(item['action_text'] or '查看详情'))
    if coverage:
        lines.extend([COVERAGE_WARNING, 'Coverage：' + ', '.join(sorted(set(coverage))).upper()])
    lines.extend([f'/detail {item["id"]}', '仅基于 Echelon 已采集信息。'])
    return '\n'.join(lines)


def render_digest(run, items, overviews, coverage, timezone):
    heading = '【📬 Echelon 补发收信】' if run['kind'] == 'catchup' else '【📬 Echelon Inbox】'
    lines = [heading, f"{stamp(run['window_end'], timezone)} 收信"]
    for priority in PRIORITY_ICON:
        group = [item for item in items if item['priority'] == priority]
        if not group:
            continue
        lines.append(f'\n{PRIORITY_ICON[priority]} {priority.upper()}')
        for item in group:
            lines.append(f"#{item['id']} {CATEGORY_ICON.get(item['category'], '💬')} {plain(item['title'])}" +
                         (' [已提前提醒]' if item['was_urgent'] else ''))
            if item['deadline_at'] is not None:
                lines.append('截止：' + stamp(item['deadline_at'], timezone))
            if item['action_required']:
                lines.append('需要：' + plain(item['action_text'] or '查看详情'))
    if not any(i['priority'] in {'high', 'critical'} for i in items):
        lines.append('\n当前已采集信息中没有识别到新的高优先级事项。')
    if overviews:
        lines.append('\n━━━ 💬 群聊概览 ━━━')
        lines.extend(overviews)
    if coverage:
        lines.append(COVERAGE_WARNING)
    lines.extend(['\n/inbox · /detail <id>', '仅基于 Echelon 已采集信息。'])
    return '\n'.join(lines)
