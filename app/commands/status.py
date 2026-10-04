import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import AppConfig
from app.onebot.actions import ActionGateway
from app.operations.diagnostics import health_warning
from app.storage.inbox_repository import InboxRepository
from app.storage.repository import Repository


async def render_status(repository: Repository, actions: ActionGateway,
                        config: AppConfig, started_at: float) -> str:
    self_id = int(await repository.state("onebot_self_id") or 0)
    warning = await health_warning(repository, actions)
    rows = await repository.query("SELECT count(*) AS n FROM messages WHERE self_id=?", (self_id,))
    counts = {row["status"]: row["n"] for row in await repository.query(
        "SELECT status,count(*) AS n FROM summary_jobs WHERE self_id=? GROUP BY status", (self_id,))}
    last = await repository.state("last_received_at") or await repository.state("last_event_time")
    last_text = (datetime.fromtimestamp(float(last), ZoneInfo(config.timezone)).strftime(
        "%Y-%m-%d %H:%M:%S") if last else "尚未收到白名单群消息")
    health = await repository.state("deepseek_health") or "Unknown"
    pending = await repository.query("SELECT count(*) AS n FROM private_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND dead_letter_at IS NULL AND (self_id=? OR self_id IS NULL)", (self_id,))
    minutes = max(0, int((time.time() - started_at) // 60))
    self_id = int(await repository.state("onebot_self_id") or 0)
    attachments = {row["download_status"]: row["n"] for row in await repository.query(
        "SELECT download_status,count(*) AS n FROM attachments WHERE self_id=? GROUP BY download_status", (self_id,))}
    unread = await InboxRepository(repository.db).unread_count(self_id)
    from app.delivery.preferences import read_delivery_preferences
    from app.delivery.renderer import stamp
    from app.delivery.schedule import next_digest
    async with repository.db.transaction() as connection:
        prefs = await read_delivery_preferences(connection, self_id)
    authorized = await repository.authorizations.list_active(self_id)
    delivery = await repository.query("SELECT last_heartbeat FROM delivery_settings WHERE self_id=?", (self_id,))
    last_beat = delivery[0]['last_heartbeat'] if delivery else None
    healthy = last_beat is not None and time.time()-last_beat <= prefs.heartbeat_seconds*2+5
    deferred = (await repository.query("SELECT count(*) AS n FROM inbox_deliveries WHERE self_id=? AND status='deferred'", (self_id,)))[0]['n']
    return (f"【Echelon】\n\nOneBot：\n{'Connected' if actions.connected else 'Disconnected'}"
            f"\n\n数据库消息：\n{rows[0]['n']}\n\n监控群：\n{len(authorized)}"
            f"\n\n最后收到消息：\n{last_text}\n\nDeepSeek：\n{health}"
            f"\n\n任务：\n{counts.get('queued', 0)} queued\n{counts.get('running', 0)} running"
            f"\n{counts.get('failed', 0)} failed\n待发私聊：{pending[0]['n']}"
            f"\n\n运行时间：\n{minutes // 60}h {minutes % 60}m"
            f"\n\n附件：\ndownloaded {attachments.get('downloaded', 0)}"
            f"\npending {attachments.get('pending', 0)}\nfailed {attachments.get('failed', 0)}"
            f"\n\n收件箱：\nunread {unread}"
            f"\n\nAuthorized Groups：{len(authorized)}\nDelivery：Heartbeat {'Healthy' if healthy else 'Waiting'}"
            f"\nNext Digest：{stamp(next_digest(prefs, time.time(), config.timezone), config.timezone)}"
            f"\nDeferred：{deferred}\nHealth：{'⚠️ /doctor 查看' if warning else '✅ OK'}\n/allow · /delivery")
