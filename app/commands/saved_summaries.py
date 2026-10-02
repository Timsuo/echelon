from app.llm.schemas import load_summary
from app.notifier.renderer import make_compact, render_compact, render_topic, time_range
from app.rendering.icons import COVERAGE_WARNING
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import read_policy


async def group_name(connection, self_id, group_id):
    policy = await read_policy(connection, self_id, group_id)
    async with connection.execute('SELECT group_name,active FROM group_authorizations WHERE self_id=? AND group_id=?', (self_id, group_id)) as cursor:
        group = await cursor.fetchone()
    return policy.alias or (group['group_name'] if group else None) or str(group_id), bool(group and group['active'])


class SavedSummaryCommands:
    def __init__(self, repository, config):
        self.repository, self.config = repository, config

    async def handle(self, event, argument):
        parts = argument.split()
        if not parts or parts[0] not in {'list', 'detail', 'compact', 'topic'}:
            return False
        if await self.repository.state('onebot_self_id') != str(event.self_id):
            raise ValueError('总结账号不匹配')
        if parts[0] == 'list':
            if len(parts) != 1:
                raise ValueError('用法：/summary list')
            summaries = await self.repository.query('SELECT * FROM summaries WHERE self_id=? ORDER BY created_at DESC,id DESC LIMIT 10', (event.self_id,))
            lines = ['【📝 最近总结】']
            for summary in summaries:
                async with self.repository.db.transaction() as connection:
                    name, active = await group_name(connection, event.self_id, summary['group_id'])
                try:
                    count = len(load_summary(summary['summary_json'], summary['schema_version']).topics)
                except (ValueError, TypeError):
                    count = '未知'
                lines.extend([f"\n#{summary['id']} · 📚 {name}",
                    ('➖ 已停止接收' if not active else ''),
                    '🕒 ' + time_range(summary['window_start'], summary['window_end'], self.config.timezone, False),
                    f"💬 {summary['source_message_count']}条 · {count}个话题", f"/summary detail {summary['id']}"])
            if not summaries:
                lines.append('暂无已保存总结。')
            await self.repository.notify('\n'.join(line for line in lines if line), event.self_id)
            return True
        if len(parts) != (3 if parts[0] == 'topic' else 2) or not parts[1].isascii() or not parts[1].isdecimal() or len(parts[1]) > 18 or int(parts[1]) <= 0:
            raise ValueError('用法：/summary detail|compact <id> 或 /summary topic <id> <序号>')
        found = await self.repository.query('SELECT * FROM summaries WHERE id=? AND self_id=?', (int(parts[1]), event.self_id))
        if not found:
            raise ValueError('总结不存在或不属于当前账号。')
        summary = found[0]
        if parts[0] == 'detail':
            text = summary['rendered_text']
        elif parts[0] == 'compact' and summary['compact_rendered_text']:
            text = summary['compact_rendered_text']
        else:
            try:
                data = load_summary(summary['summary_json'], summary['schema_version'])
            except (ValueError, TypeError):
                if parts[0] == 'topic':
                    raise ValueError('旧总结没有可展开的话题结构，请使用 /summary detail。') from None
                text = f"【📝 旧总结 #{summary['id']}】\n消息：{summary['source_message_count']}条\n旧结构无法压缩，完整内容已保留：\n/summary detail {summary['id']}"
                if await HistoryRepository(self.repository, self.config).unresolved(event.self_id, summary['group_id'], summary['window_start'], summary['window_end']):
                    text += '\n' + COVERAGE_WARNING
            else:
                if parts[0] == 'topic':
                    if not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 3 or not 1 <= int(parts[2]) <= len(data.topics):
                        raise ValueError('话题序号不存在。')
                    text = render_topic(data.topics[int(parts[2])-1], int(parts[2]), self.config.timezone)
                else:
                    gaps = await HistoryRepository(self.repository, self.config).unresolved(event.self_id, summary['group_id'], summary['window_start'], summary['window_end'])
                    compact = make_compact(data, self.config.summary, bool(gaps))
                    async with self.repository.db.transaction() as connection:
                        name, _ = await group_name(connection, event.self_id, summary['group_id'])
                    text = render_compact(compact, summary | {'group_name': name}, summary['source_message_count'], self.config.timezone)
                    await self.repository.query('UPDATE summaries SET compact_json=?,compact_rendered_text=? WHERE id=? AND self_id=?',
                        (compact.model_dump_json(), text, summary['id'], event.self_id))
        await self.repository.notify(text, event.self_id)
        return True
