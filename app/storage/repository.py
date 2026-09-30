import time
from typing import Any

from app.storage.db import Database


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        async with self.db.transaction() as connection:
            async with connection.execute(sql, args) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def state(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            await self.query("INSERT INTO runtime_state VALUES (?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            return value
        rows = await self.query("SELECT value FROM runtime_state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    async def add_message(self, record: dict[str, Any]) -> bool:
        columns = ("self_id", "group_id", "message_id", "user_id", "nickname", "event_time",
                   "received_time", "raw_message", "normalized_text", "reply_to_message_id",
                   "message_json")
        async with self.db.transaction() as connection:
            async with connection.execute(
                f"INSERT INTO messages ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                "ON CONFLICT(self_id,group_id,message_id) DO NOTHING",
                tuple(record[column] for column in columns),
            ) as cursor:
                inserted = cursor.rowcount == 1
            await connection.execute(
                "INSERT INTO runtime_state VALUES ('last_event_time',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(record["received_time"]),))
            return inserted

    async def bind_onebot(self, self_id: int) -> bool:
        # First-account binding and comparison must be atomic across concurrent handshakes.
        async with self.db.transaction() as connection:
            await connection.execute(
                "INSERT INTO runtime_state VALUES ('onebot_self_id',?) ON CONFLICT DO NOTHING",
                (str(self_id),))
            async with connection.execute(
                "SELECT value FROM runtime_state WHERE key='onebot_self_id'"
            ) as cursor:
                row = await cursor.fetchone()
                return row["value"] == str(self_id)

    @staticmethod
    async def enqueue_text(connection: Any, text: str) -> None:
        # Plain text segments are used at delivery; split without interpreting CQ codes.
        parts = [text[i:i + 1800] for i in range(0, len(text), 1800)]
        for index, part in enumerate(parts, 1):
            prefix = f"({index}/{len(parts)})\n" if len(parts) > 1 else ""
            await connection.execute(
                "INSERT INTO private_outbox(text,created_at) VALUES (?,?)", (prefix + part, time.time()))

    async def notify(self, text: str) -> None:
        async with self.db.transaction() as connection:
            await self.enqueue_text(connection, text)

    async def queue_summaries(self, self_id: int, message_id: str, groups: list[int],
                              start: float, end: float) -> list[int]:
        async with self.db.transaction() as connection:
            async with connection.execute(
                "SELECT value FROM runtime_state WHERE key='onebot_self_id'"
            ) as cursor:
                bound = await cursor.fetchone()
            if bound is None or bound["value"] != str(self_id):
                raise ValueError("总结账号与当前绑定账号不一致")
            async with connection.execute(
                "INSERT INTO command_receipts VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (self_id, message_id, time.time()),
            ) as cursor:
                if cursor.rowcount == 0:
                    return []
            async with connection.execute(
                "SELECT count(*) FROM summary_jobs WHERE status IN ('queued','running')"
            ) as cursor:
                row = await cursor.fetchone()
                if row[0] + len(groups) > 100:
                    await self.enqueue_text(connection, "总结队列已满，请稍后重试。")
                    return []
            ids = []
            for group in groups:
                async with connection.execute(
                    "INSERT INTO summary_jobs(self_id,group_id,window_start,window_end,status,created_at) "
                    "VALUES (?,?,?,?,'queued',?)", (self_id, group, start, end, time.time())
                ) as cursor:
                    ids.append(cursor.lastrowid)
            await self.enqueue_text(connection, f"总结已排队，任务：{', '.join(map(str, ids))}")
            return ids

    async def claim_job(self) -> dict[str, Any] | None:
        rows = await self.query(
            "UPDATE summary_jobs SET status='running',started_at=? WHERE id="
            "(SELECT id FROM summary_jobs WHERE status='queued' ORDER BY id LIMIT 1) "
            "AND status='queued' RETURNING *", (time.time(),))
        return rows[0] if rows else None

    async def recover(self) -> None:
        # Called only while holding the application-wide exclusive process lock.
        await self.query("UPDATE summary_jobs SET status='queued',started_at=NULL,"
                         "error='Recovered after interrupted process' WHERE status='running'")

    async def window_messages(self, self_id: int, group: int, start: float, end: float,
                              limit: int) -> list[dict[str, Any]]:
        return await self.query(
            "SELECT * FROM messages WHERE self_id=? AND group_id=? AND event_time>=? AND event_time<? "
            "ORDER BY event_time,id LIMIT ?", (self_id, group, start, end, limit))

    async def complete_job(self, job: dict, model: str, count: int,
                           summary_json: str, text: str) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "INSERT INTO summaries(job_id,self_id,group_id,window_start,window_end,model,"
                "source_message_count,summary_json,rendered_text,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job["id"], job["self_id"], job["group_id"], job["window_start"], job["window_end"], model,
                 count, summary_json, text, time.time()))
            await connection.execute(
                "UPDATE summary_jobs SET status='completed',completed_at=?,error=NULL WHERE id=?",
                (time.time(), job["id"]))
            await self.enqueue_text(connection, text)

    async def fail_job(self, job_id: int, error: str) -> None:
        async with self.db.transaction() as connection:
            await connection.execute(
                "UPDATE summary_jobs SET status='failed',completed_at=?,error=? WHERE id=?",
                (time.time(), error, job_id))
            await self.enqueue_text(connection, f"总结任务 #{job_id} 失败：{error}。群消息仍保留。")

    async def record_retry(self, job_id: int) -> None:
        await self.query("UPDATE summary_jobs SET retry_count=retry_count+1 WHERE id=?", (job_id,))

    async def next_notification(self) -> dict | None:
        # One sender per process, guaranteed by process lock. Keep chunk ordering.
        rows = await self.query("SELECT * FROM private_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 1")
        return rows[0] if rows and rows[0]["next_attempt"] <= time.time() else None

    async def notification_result(self, item: dict, error: str | None = None) -> None:
        if error is None:
            await self.query("UPDATE private_outbox SET sent_at=?,error=NULL WHERE id=?",
                             (time.time(), item["id"]))
        else:
            delay = min(300, 2 ** min(item["attempts"] + 1, 9))
            await self.query("UPDATE private_outbox SET attempts=attempts+1,error=?,next_attempt=? "
                             "WHERE id=?", (error, time.time() + delay, item["id"]))
