import logging
import time

from app.config import AppConfig
from app.history.renderer import recovery_report
from app.storage.authorization_repository import active_ids, is_active
from app.storage.policy_repository import read_policy
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


async def rows(connection, sql: str, args: tuple = ()) -> list[dict]:
    async with connection.execute(sql, args) as cursor:
        return [dict(row) for row in await cursor.fetchall()]


async def state(connection, key: str, value: str) -> None:
    await connection.execute("INSERT INTO runtime_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


class HistoryRepository:
    def __init__(self, repository: Repository, config: AppConfig) -> None:
        self.repository = repository
        self.db = repository.db
        self.config = config

    async def _gap(self, connection, self_id: int, start: float, reason: str, now: float) -> None:
        await connection.execute("INSERT INTO collection_gaps(self_id,started_at,reason,created_at,updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING", (self_id, min(start, now), reason, now, now))

    async def startup(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            values = {r['key']: r['value'] for r in await rows(connection, "SELECT * FROM runtime_state")}
            self_id = int(values.get("onebot_self_id", "0"))
            if self_id and "last_ws_connected" in values:
                clean = values.get("service_clean_shutdown") == "true"
                if clean and "last_service_stop" in values:
                    start = float(values["last_service_stop"])
                else:
                    start = max(float(values.get(key, "0")) for key in
                                ("last_collection_checkpoint", "last_realtime_received_at", "last_event_time", "last_ws_connected"))
                await self._gap(connection, self_id, start, "offline_window_clean" if clean else "offline_window_unclean", now)
            await state(connection, "service_clean_shutdown", "false")
            await state(connection, "onebot_connected", "false")
            await connection.execute("UPDATE history_sync_jobs SET status='queued' WHERE status='running'")
            await connection.execute("UPDATE collection_gaps SET recovery_status='pending' WHERE recovery_status='running'")

    async def stop(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            values = {r['key']: r['value'] for r in await rows(connection, "SELECT * FROM runtime_state")}
            if values.get("onebot_self_id") and values.get("last_ws_connected"):
                self_id = int(values["onebot_self_id"])
                await self._gap(connection, self_id, now, "service_stop", now)
                # Uvicorn closes WebSockets before lifespan shutdown. Label that short tail as intentional.
                await connection.execute("UPDATE collection_gaps SET reason='service_stop',updated_at=? "
                    "WHERE self_id=? AND ended_at IS NULL AND started_at>=?", (now, self_id, now - 5))
            await state(connection, "last_service_stop", str(now))
            await state(connection, "service_clean_shutdown", "true")
            await state(connection, "onebot_connected", "false")

    async def disconnected(self, self_id: int, session: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            values = {r['key']: r['value'] for r in await rows(connection, "SELECT * FROM runtime_state")}
            if values.get("onebot_session") != session or values.get("onebot_connected") != "true":
                return
            await self._gap(connection, self_id, now, "unexpected_disconnect", now)
            await state(connection, "last_ws_disconnected", str(now))
            await state(connection, "onebot_connected", "false")
            logger.info("Collection gap opened self_id=%s", self_id)

    async def _enqueue(self, connection, self_id: int, group_id: int, mode: str,
                       start: float, end: float, now: float, gap_id: int | None = None) -> int | None:
        if not await is_active(connection, self_id, group_id):
            return None
        result = await rows(connection, "INSERT INTO history_sync_jobs(self_id,group_id,gap_id,mode,window_start,window_end,next_attempt,created_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id",
            (self_id, group_id, gap_id, mode, start, end, now + self.config.history.debounce_seconds if gap_id else now, now))
        return result[0]["id"] if result else None

    async def connected(self, self_id: int, session: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            values = {r['key']: r['value'] for r in await rows(connection, "SELECT * FROM runtime_state")}
            if values.get("onebot_connected") == "true" and values.get("onebot_session") != session:
                # A new handshake may beat the old socket's asynchronous disconnect write.
                # Do not let stale-session protection erase that gap: infer its conservative start here.
                start = max(float(values.get(key, "0")) for key in
                            ("last_collection_checkpoint", f"last_realtime_received_at:{self_id}", "last_ws_connected"))
                await self._gap(connection, self_id, start, "unexpected_disconnect", now)
            await state(connection, "onebot_session", session)
            await state(connection, "onebot_connected", "true")
            await state(connection, "last_ws_connected", str(now))
            await state(connection, "last_collection_checkpoint", str(now))
            gaps = await rows(connection, "UPDATE collection_gaps SET ended_at=?,updated_at=?,report_after=? "
                "WHERE self_id=? AND ended_at IS NULL RETURNING *", (now, now, now + self.config.history.debounce_seconds, self_id))
            for group_id in await active_ids(connection, self_id):
                await connection.execute("INSERT INTO history_sync_state(self_id,group_id,last_scheduled_at) VALUES (?,?,?) "
                                         "ON CONFLICT DO NOTHING", (self_id, group_id, now))
                policy = await read_policy(connection, self_id, group_id)
                if self.config.history.enabled and policy.mode != "ignore":
                    for gap in gaps:
                        await self._enqueue(connection, self_id, group_id, "reconnect", gap["started_at"], now, now, gap["id"])
            # Reconnection flaps share a quiet period, but each gap retains its own time window.
            await connection.execute("UPDATE collection_gaps SET report_after=? WHERE self_id=? AND reported_at IS NULL",
                                     (now + self.config.history.debounce_seconds, self_id))
            for gap in gaps:
                await self._aggregate(connection, gap["id"], now)

    async def manual(self, self_id: int, group_id: int | None = None) -> list[int]:
        if not self.config.history.enabled:
            raise ValueError("历史核验已关闭，请检查 history.enabled")
        now, ids = time.time(), []
        async with self.db.transaction() as connection:
            bound = await rows(connection, "SELECT value FROM runtime_state WHERE key='onebot_self_id'")
            if not bound or bound[0]["value"] != str(self_id):
                raise ValueError("当前机器人账号不匹配")
            groups = await active_ids(connection, self_id) if group_id is None else [group_id]
            for group in groups:
                if not await is_active(connection, self_id, group):
                    raise ValueError("该群当前未授权采集。")
                policy = await read_policy(connection, self_id, group)
                if policy.mode == "ignore":
                    continue
                interval = self.config.history.interval(policy.mode)
                identifier = await self._enqueue(connection, self_id, group, "manual", max(0, now - interval), now, now)
                if identifier:
                    ids.append(identifier)
            await Repository.enqueue_text(connection, "已创建同步任务：" + (", ".join(map(str, ids)) or "无（已排队或群已忽略）") +
                                          "。仅有限回查最近历史，不保证完整恢复。", self_id)
        return ids

    async def schedule(self, self_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            await state(connection, "last_collection_checkpoint", str(now))
            if not self.config.history.enabled or not self.config.history.periodic_enabled:
                return
            for group in await active_ids(connection, self_id):
                policy = await read_policy(connection, self_id, group)
                interval = self.config.history.interval(policy.mode)
                if interval is None:
                    continue
                records = await rows(connection, "SELECT * FROM history_sync_state WHERE self_id=? AND group_id=?", (self_id, group))
                previous = records[0]["last_scheduled_at"] if records else now
                await connection.execute("INSERT INTO history_sync_state(self_id,group_id,last_scheduled_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
                                         (self_id, group, previous))
                if now - previous >= interval:
                    await self._enqueue(connection, self_id, group, "periodic", max(0, previous), now, now)
                    await connection.execute("UPDATE history_sync_state SET last_scheduled_at=? WHERE self_id=? AND group_id=?", (now, self_id, group))

    async def claim(self, self_id: int) -> dict | None:
        now = time.time()
        records = await self.repository.query("UPDATE history_sync_jobs SET status='running',attempts=attempts+1,started_at=? "
            "WHERE id=(SELECT id FROM history_sync_jobs WHERE self_id=? AND status='queued' AND next_attempt<=? "
            "AND EXISTS (SELECT 1 FROM group_authorizations a WHERE a.self_id=history_sync_jobs.self_id AND a.group_id=history_sync_jobs.group_id AND a.active=1) "
            "ORDER BY CASE mode WHEN 'reconnect' THEN 0 WHEN 'manual' THEN 1 ELSE 2 END,id LIMIT 1) AND status='queued' RETURNING *",
            (now, self_id, now))
        return records[0] if records else None

    @staticmethod
    async def _aggregate(connection, gap_id: int, now: float) -> None:
        jobs = await rows(connection, "SELECT status,coverage FROM history_sync_jobs WHERE gap_id=?", (gap_id,))
        if any(j["status"] in {"queued", "running"} for j in jobs):
            coverage = "running"
        elif not jobs:
            coverage = "unknown"
        else:
            states = {j["coverage"] for j in jobs}
            coverage = next((value for value in ("failed", "unknown", "partial") if value in states), "likely_covered")
        await connection.execute("UPDATE collection_gaps SET recovery_status=?,updated_at=? WHERE id=?", (coverage, now, gap_id))

    async def finish(self, job: dict, coverage: str, received: int = 0, inserted: int = 0,
                     invalid: int = 0, oldest: int | None = None, newest: int | None = None,
                     error: str | None = None, retry: bool = False) -> None:
        now = time.time()
        status = "queued" if retry else ("failed" if error else "completed")
        async with self.db.transaction() as connection:
            current = await rows(connection, 'SELECT status,attempts FROM history_sync_jobs WHERE id=? AND self_id=?', (job['id'], job['self_id']))
            if not current or current[0]['status'] != 'running' or current[0]['attempts'] != job['attempts']:
                return
            if not await is_active(connection, job["self_id"], job["group_id"]):
                retry, status, coverage, error = False, "failed", "failed", "authorization_removed"
            await connection.execute("UPDATE history_sync_jobs SET status=?,coverage=?,error=?,next_attempt=?,completed_at=?,"
                "messages_received=messages_received+?,messages_inserted=messages_inserted+?,invalid_messages=invalid_messages+?,"
                "oldest_message_time=?,newest_message_time=? WHERE id=? AND self_id=?",
                (status, coverage, error, now + min(60, 5 * 2 ** min(job["attempts"], 4)), None if retry else now,
                 received, inserted, invalid, oldest, newest, job["id"], job["self_id"]))
            if not retry:
                await connection.execute("INSERT INTO history_sync_state(self_id,group_id,last_scheduled_at,last_history_sync_at,"
                    "last_history_sync_result,last_history_oldest_event,last_history_newest_event,last_periodic_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(self_id,group_id) DO UPDATE SET last_history_sync_at=excluded.last_history_sync_at,"
                    "last_history_sync_result=excluded.last_history_sync_result,last_history_oldest_event=excluded.last_history_oldest_event,"
                    "last_history_newest_event=excluded.last_history_newest_event,last_periodic_at=COALESCE(excluded.last_periodic_at,last_periodic_at)",
                    (job["self_id"], job["group_id"], now, now, coverage, oldest, newest, now if job["mode"] == "periodic" else None))
                if job["mode"] == "manual":
                    await Repository.enqueue_text(connection, f"历史核验 #{job['id']} 群 {job['group_id']}：{coverage.upper()}\n"
                        f"{job['messages_inserted'] + inserted} new / {job['messages_received'] + received} fetched\n"
                        "仅有限 best-effort 回查，不能保证全部历史完整。", job["self_id"])
            if job["gap_id"]:
                await self._aggregate(connection, job["gap_id"], now)

    async def report_ready(self, self_id: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            gaps = await rows(connection, "SELECT * FROM collection_gaps WHERE self_id=? AND reported_at IS NULL ORDER BY id", (self_id,))
            if not gaps or any(g["ended_at"] is None or g["report_after"] > now or g["recovery_status"] in {"pending", "running"} for g in gaps):
                return
            jobs = await rows(connection, "SELECT j.* FROM history_sync_jobs j JOIN collection_gaps g ON g.id=j.gap_id "
                "WHERE g.self_id=? AND g.reported_at IS NULL", (self_id,))
            await Repository.enqueue_text(connection, recovery_report(gaps, jobs, self.config.timezone), self_id)
            await connection.execute("UPDATE collection_gaps SET reported_at=? WHERE self_id=? AND reported_at IS NULL", (now, self_id))

    async def unresolved(self, self_id: int, group_id: int, start: float, end: float) -> list[dict]:
        # Shared boundary for summaries and future Inbox/triage coverage checks; no LLM involvement.
        async with self.db.transaction() as connection:
            return await self.unresolved_on(connection, self_id, group_id, start, end)

    @staticmethod
    async def unresolved_on(connection, self_id, group_id, start, end):
        return await rows(connection, "SELECT g.*,COALESCE(j.coverage,'unknown') AS coverage FROM collection_gaps g "
            "LEFT JOIN history_sync_jobs j ON j.gap_id=g.id AND j.self_id=g.self_id AND j.group_id=? "
            "WHERE g.self_id=? AND g.started_at<? AND COALESCE(g.ended_at,?)>? "
            "AND (j.coverage IS NULL OR j.coverage!='likely_covered' OR j.status!='completed') ORDER BY g.started_at",
            (group_id, self_id, end, end, start))
