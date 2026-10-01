import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import AppConfig
from app.onebot.actions import ActionGateway
from app.storage.inbox_repository import InboxRepository
from app.storage.repository import Repository


async def render_status(repository: Repository, actions: ActionGateway,
                        config: AppConfig, started_at: float) -> str:
    rows = await repository.query("SELECT count(*) AS n FROM messages")
    counts = {row["status"]: row["n"] for row in await repository.query(
        "SELECT status,count(*) AS n FROM summary_jobs GROUP BY status")}
    last = await repository.state("last_event_time")
    last_text = (datetime.fromtimestamp(float(last), ZoneInfo(config.timezone)).strftime(
        "%Y-%m-%d %H:%M:%S") if last else "尚未收到白名单群消息")
    health = await repository.state("deepseek_health") or "Unknown"
    pending = await repository.query("SELECT count(*) AS n FROM private_outbox WHERE sent_at IS NULL")
    minutes = max(0, int((time.time() - started_at) // 60))
    self_id = int(await repository.state("onebot_self_id") or 0)
    attachments = {row["download_status"]: row["n"] for row in await repository.query(
        "SELECT download_status,count(*) AS n FROM attachments WHERE self_id=? GROUP BY download_status", (self_id,))}
    unread = await InboxRepository(repository.db).unread_count(self_id)
    return (f"【Echelon】\n\nOneBot：\n{'Connected' if actions.connected else 'Disconnected'}"
            f"\n\n数据库消息：\n{rows[0]['n']}\n\n监控群：\n{len(config.groups.allowed)}"
            f"\n\n最后收到消息：\n{last_text}\n\nDeepSeek：\n{health}"
            f"\n\n任务：\n{counts.get('queued', 0)} queued\n{counts.get('running', 0)} running"
            f"\n{counts.get('failed', 0)} failed\n待发私聊：{pending[0]['n']}"
            f"\n\n运行时间：\n{minutes // 60}h {minutes % 60}m"
            f"\n\n附件：\ndownloaded {attachments.get('downloaded', 0)}"
            f"\npending {attachments.get('pending', 0)}\nfailed {attachments.get('failed', 0)}"
            f"\n\n收件箱：\nunread {unread}")
