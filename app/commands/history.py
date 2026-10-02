from app.config import AppConfig
from app.history.renderer import stamp
from app.onebot.actions import ActionGateway
from app.onebot.adapter import MessageEvent, parse_self_id
from app.storage.history_repository import HistoryRepository
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository


class HistoryCommands:
    def __init__(self, repository: Repository, config: AppConfig, actions: ActionGateway) -> None:
        self.repository = repository
        self.config = config
        self.actions = actions
        self.history = HistoryRepository(repository, config)

    async def account(self, event: MessageEvent) -> None:
        if await self.repository.state("onebot_self_id") != str(event.self_id):
            raise ValueError("当前机器人账号不匹配")

    async def sync(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        try:
            group_id = parse_self_id(argument) if argument else None
        except ValueError:
            raise ValueError("用法：/sync 或 /sync <群号>；不支持全量历史下载") from None
        await self.history.manual(event.self_id, group_id)

    async def coverage(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        if argument:
            raise ValueError("用法：/coverage")
        sid, zone = event.self_id, self.config.timezone
        last = await self.repository.state(f"last_realtime_received_at:{sid}")
        gaps = await self.repository.query("SELECT * FROM collection_gaps WHERE self_id=? ORDER BY id DESC LIMIT 5", (sid,))
        pending = await self.repository.query("SELECT count(*) AS n FROM collection_gaps WHERE self_id=? "
                                             "AND recovery_status!='likely_covered'", (sid,))
        lines = ["【Echelon Collection Coverage】", f"OneBot：{'Connected' if self.actions.connected else 'Disconnected'}",
                 f"最后实时消息接收：{stamp(float(last) if last else None, zone)}",
                 f"未确认缺口：{pending[0]['n']}"]
        for gap in gaps:
            counts = (await self.repository.query("SELECT COALESCE(sum(messages_received),0) AS fetched,"
                "COALESCE(sum(messages_inserted),0) AS added FROM history_sync_jobs WHERE gap_id=? AND self_id=?", (gap["id"], sid)))[0]
            lines.extend([f"\n断连 #{gap['id']}：{stamp(gap['started_at'], zone)} — {stamp(gap['ended_at'], zone)}",
                          f"原因：{gap['reason']}；Recovery：{gap['recovery_status'].upper()}",
                          f"回补：{counts['added']} new / {counts['fetched']} fetched"])
        if not gaps:
            lines.append("尚无已记录断连；这不代表没有漏报。")
        lines.append("\n最近周期核验：")
        for policy in await PolicyRepository(self.repository.db).listing(sid):
            records = await self.repository.query("SELECT * FROM history_sync_state WHERE self_id=? AND group_id=?", (sid, policy.group_id))
            row = records[0] if records else {}
            lines.append(f"{policy.alias or policy.group_id}：{stamp(row.get('last_periodic_at'), zone)}；"
                         f"最近回查：{row.get('last_history_sync_result') or 'unknown'}")
        lines.extend(["\n历史回查仅 best effort，无法保证绝对完整。", "/help coverage\n/sync"])
        await self.repository.notify("\n".join(lines), sid)
