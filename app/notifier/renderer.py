import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.history.renderer import coverage_warning
from app.llm.schemas import CompactSummaryData, CompactTopic
from app.rendering.icons import CATEGORY_ICON, COVERAGE_WARNING, SUMMARY_ICON, plain


def time_range(start, end, timezone, duration=True):
    zone = ZoneInfo(timezone)
    left, right = datetime.fromtimestamp(start, zone), datetime.fromtimestamp(end, zone)
    if left.date() == right.date():
        text = f'{left:%H:%M}–{right:%H:%M}'
    else:
        fmt = '%Y年%m月%d日 %H:%M' if left.year != right.year else '%m月%d日 %H:%M'
        text = f'{left.strftime(fmt)} – {right.strftime(fmt)}'
    if duration:
        seconds = int(end-start)
        text += f' · {seconds//60}分钟' + (f'{seconds%60}秒' if seconds%60 else '')
    return text


def participants(names):
    names = list(dict.fromkeys(plain(name) for name in names))
    return '、'.join(names) if len(names) <= 5 else '、'.join(names[:3]) + f'等 {len(names)} 人'


def render_topic(topic, index, timezone, detailed=True):
    number = '①②③④⑤⑥⑦⑧⑨⑩'[index-1] if index <= 10 else str(index)+'.'
    lines = [f"{number} {CATEGORY_ICON.get(getattr(topic, 'category', 'discussion'), '💬')} {plain(topic.title)}"]
    if getattr(topic, 'continuation', False):
        lines.append('🔄 延续话题（仅本窗口证据）')
    if getattr(topic, 'topic_start', None) is not None:
        lines.append('🕒 ' + time_range(topic.topic_start, topic.topic_end, timezone))
    lines.append(plain(topic.summary))
    if detailed and topic.participants:
        lines.append('👥 参与：' + participants(topic.participants))
    if detailed and getattr(topic, 'start_message_id', None):
        refs = list(dict.fromkeys([topic.start_message_id, topic.end_message_id, *topic.notable_message_ids]))
        lines.append('🔎 参考消息：' + '、'.join(refs))
    return '\n'.join(lines)


def header(job, count, timezone, topic_count, compact=False):
    return [f"【{SUMMARY_ICON} Echelon {'群聊总结' if compact else '完整总结'} #{job['id']}】",
            f"📚 群：{job.get('group_name') or job['group_id']}",
            '🕒 时间：' + time_range(job['window_start'], job['window_end'], timezone, False),
            f'💬 消息：{count} 条 · {topic_count} 个话题']


def sections(data):
    lines = []
    for label, items in (('✅ 决定', data.decisions), ('📋 待办', data.todos),
                         ('📌 重要事件', data.important_events), ('❓ 不确定事项', data.uncertainties)):
        if items:
            lines.append('\n━━━ ' + label + ' ━━━')
            lines.extend('• ' + plain(item) for item in items)
    return lines


def render_summary(data, job: dict, count: int, timezone: str, gaps=None) -> str:
    lines = header(job, count, timezone, len(data.topics))
    if count == 0:
        lines.append('此时间窗口没有已采集的新消息。')
    if data.topics:
        lines.append('\n━━━ 💬 讨论话题 ━━━')
        lines.extend('\n' + render_topic(topic, i, timezone) for i, topic in enumerate(data.topics, 1))
    lines.extend(sections(data))
    if data.notable_message_ids:
        lines.append('\n🔎 参考消息 ID：' + '、'.join(data.notable_message_ids))
    if gaps:
        lines.extend(['', coverage_warning(gaps, timezone)])
    lines.append('\n仅基于本地已采集记录；总结可能存在误差。')
    return '\n'.join(lines)


def make_compact(data, settings, coverage=False):
    # Extract whole sentences, never a prefix of a serialized/rendered document.
    ranked = sorted(enumerate(data.topics), key=lambda pair: ({'high': 0, 'normal': 1, 'low': 2}.get(getattr(pair[1], 'importance', 'normal'), 1), pair[0]))
    selected = []
    for _, topic in ranked[:settings.compact_max_topics]:
        sentence = re.split(r'(?<=[。！？!?])\s*|\n+', topic.summary, maxsplit=1)[0] or topic.summary
        selected.append(CompactTopic(title=topic.title, summary=sentence,
            importance=getattr(topic, 'importance', 'normal'), category=getattr(topic, 'category', 'discussion'),
            topic_start=getattr(topic, 'topic_start', None), topic_end=getattr(topic, 'topic_end', None),
            continuation=getattr(topic, 'continuation', False)))
    compact = CompactSummaryData(headline='当前窗口重点', key_topics=selected, decisions=data.decisions, todos=data.todos,
        important_events=data.important_events, uncertainties=data.uncertainties,
        omitted_topic_count=len(data.topics)-len(selected), coverage_warning_present=coverage)
    # Soft target: actionable facts remain complete even when those alone exceed it.
    fixed = sum(len(v) for field in ('decisions', 'todos', 'important_events', 'uncertainties') for v in getattr(compact, field)) + 350
    while len(compact.key_topics) > 1 and fixed + sum(len(t.title)+len(t.summary)+80 for t in compact.key_topics) > settings.compact_target_chars:
        compact.key_topics.pop()
        compact.omitted_topic_count += 1
    if compact.key_topics and fixed + sum(len(t.title)+len(t.summary)+80 for t in compact.key_topics) > settings.compact_target_chars:
        for topic in compact.key_topics:
            topic.summary = '详细讨论已保存在完整总结中。'
    return compact


def fallback_compact(data, coverage):
    return CompactSummaryData(headline='完整总结已保存，请展开查看', key_topics=[], decisions=data.decisions,
        todos=data.todos, important_events=data.important_events, uncertainties=data.uncertainties,
        omitted_topic_count=len(data.topics), coverage_warning_present=coverage)


def render_compact(data, job, count, timezone):
    lines = header(job, count, timezone, len(data.key_topics)+data.omitted_topic_count, True)
    if count == 0:
        lines.append('此时间窗口没有已采集的新消息。')
    if data.key_topics:
        lines.append('\n━━━ 📌 重点话题 ━━━')
        lines.extend('\n'+render_topic(t, i, timezone, False) for i, t in enumerate(data.key_topics, 1))
    lines.extend(sections(data))
    if data.omitted_topic_count:
        lines.append(f'\n另有 {data.omitted_topic_count} 个话题已折叠。')
    lines.append(f"\n🔎 查看完整总结：\n/summary detail {job['id']}")
    if data.coverage_warning_present:
        lines.append(COVERAGE_WARNING)
    lines.append('仅基于 Echelon 已采集数据；总结可能存在误差。')
    return '\n'.join(lines)
