import json
import logging
import time

from app.policies.models import POLICY_FIELDS, ConfigIntent, GroupPolicy
from app.storage.db import Database
from app.storage.repository import Repository

logger = logging.getLogger(__name__)
enqueue_text = Repository.enqueue_text


async def read_policy(connection, self_id: int, group_id: int) -> GroupPolicy:
    async with connection.execute("SELECT * FROM group_policies WHERE self_id=? AND group_id=?",
                                   (self_id, group_id)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return GroupPolicy(self_id=self_id, group_id=group_id)
    fields = {key: row[key] for key in POLICY_FIELDS}
    for key in POLICY_FIELDS:
        if key.endswith("_enabled"):
            fields[key] = bool(fields[key])
    return GroupPolicy(self_id=self_id, group_id=group_id, **fields)


async def save_policy(connection, policy: GroupPolicy) -> None:
    fields = ",".join(POLICY_FIELDS)
    updates = ",".join(f"{key}=excluded.{key}" for key in POLICY_FIELDS)
    now = time.time()
    await connection.execute(
        f"INSERT INTO group_policies(self_id,group_id,{fields},created_at,updated_at) "
        f"VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(self_id,group_id) DO UPDATE SET {updates},updated_at=excluded.updated_at",
        (policy.self_id, policy.group_id, *(getattr(policy, key) for key in POLICY_FIELDS), now, now))


def render_proposal(identifier: int, data: dict) -> str:
    lines = ["【Echelon 配置变更】", f"群：{data['group_id']}"]
    for key, value in data["after"].items():
        lines.append(f"{key}: {data['before'][key]} → {value}")
    lines.extend(["10 分钟内确认应用：", f"/confirm {identifier}", f"/cancel {identifier}"])
    if data["after"].get("priority_watch_enabled"):
        lines.append("Phase 2 只保存 Priority Watch 设置；即时高优先级提醒将在 Phase 4 启用。")
    return "\n".join(lines)


class PolicyRepository:
    def __init__(self, db: Database, allowed: list[int] | tuple[int, ...]) -> None:
        self.db = db
        self.allowed = frozenset(allowed)

    def check_group(self, group_id: int) -> None:
        if group_id not in self.allowed:
            raise ValueError("该群目前不在 Echelon 采集白名单中。请先在 config.yaml 中加入该群并重启。")

    async def get(self, self_id: int, group_id: int) -> GroupPolicy:
        self.check_group(group_id)
        async with self.db.transaction() as connection:
            return await read_policy(connection, self_id, group_id)

    async def listing(self, self_id: int) -> list[GroupPolicy]:
        async with self.db.transaction() as connection:
            return [await read_policy(connection, self_id, group_id) for group_id in sorted(self.allowed)]

    async def _account(self, connection, self_id: int) -> None:
        async with connection.execute("SELECT value FROM runtime_state WHERE key='onebot_self_id'") as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] != str(self_id):
            raise ValueError("机器人账号不匹配，未应用配置")

    async def propose(self, self_id: int, admin_qq: int, group_id: int, intent: ConfigIntent,
                      request_id: int | None = None) -> int | None:
        self.check_group(group_id)
        async with self.db.transaction() as connection:
            await self._account(connection, self_id)
            current = await read_policy(connection, self_id, group_id)
            after = {key: value for key, value in intent.changes.expanded().items()
                     if getattr(current, key) != value}
            if after:
                data = dict(group_id=group_id, before={key: getattr(current, key) for key in after}, after=after)
                now = time.time()
                async with connection.execute("INSERT INTO configuration_proposals"
                    "(self_id,admin_qq,intent_json,created_at,expires_at) VALUES (?,?,?,?,?)",
                    (self_id, admin_qq, json.dumps(data, ensure_ascii=False), now, now + 600)) as cursor:
                    identifier = cursor.lastrowid
                await enqueue_text(connection, render_proposal(identifier, data), self_id)
                logger.info("Configuration proposal created id=%s group=%s", identifier, group_id)
            else:
                identifier = None
                await enqueue_text(connection, "配置已经符合要求，无需变更。", self_id)
            if request_id is not None:
                await connection.execute("UPDATE configuration_requests SET status='completed' WHERE id=? AND self_id=?",
                                         (request_id, self_id))
            return identifier

    async def resolve(self, self_id: int, admin_qq: int, identifier: int, confirm: bool) -> str:
        async with self.db.transaction() as connection:
            await self._account(connection, self_id)
            async with connection.execute("SELECT * FROM configuration_proposals WHERE id=? AND self_id=? AND admin_qq=?",
                                           (identifier, self_id, admin_qq)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("配置提案不存在")
            if row["status"] != "pending":
                return "该提案已处理：" + row["status"]
            if row["expires_at"] <= time.time():
                await connection.execute("UPDATE configuration_proposals SET status='expired' WHERE id=?", (identifier,))
                return "配置提案已过期，请重新提交 /config。"
            if confirm:
                data = json.loads(row["intent_json"])
                self.check_group(data["group_id"])
                current = await read_policy(connection, self_id, data["group_id"])
                if any(getattr(current, key) != value for key, value in data["before"].items()):
                    await connection.execute("UPDATE configuration_proposals SET status='expired' WHERE id=?", (identifier,))
                    return "配置已发生变化，该提案已失效，请重新提交 /config。"
                # Apply the displayed diff only. Never expand a profile a second time here.
                updated = GroupPolicy.model_validate(current.model_dump() | data["after"])
                await save_policy(connection, updated)
            status = "confirmed" if confirm else "cancelled"
            await connection.execute("UPDATE configuration_proposals SET status=? WHERE id=?", (status, identifier))
            logger.info("Configuration proposal %s id=%s", status, identifier)
            return "配置已更新。" if confirm else "配置提案已取消。"

    async def queue(self, self_id: int, admin_qq: int, group_id: int, message_id: str, text: str) -> None:
        self.check_group(group_id)
        async with self.db.transaction() as connection:
            await self._account(connection, self_id)
            async with connection.execute("SELECT count(*) FROM configuration_requests WHERE status IN ('queued','running')") as cursor:
                if (await cursor.fetchone())[0] >= 20:
                    raise ValueError("配置解析队列已满，请稍后再试")
            await connection.execute("INSERT INTO configuration_requests(self_id,admin_qq,group_id,message_id,input_text,created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", (self_id, admin_qq, group_id, message_id, text, time.time()))
            await enqueue_text(connection, "正在解析配置意图；完成后将发送提案，确认前不会修改配置。", self_id)

    async def recover(self) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE configuration_requests SET status='queued' WHERE status='running'")

    async def claim(self) -> dict | None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE configuration_proposals SET status='expired' WHERE status='pending' AND expires_at<=?",
                                     (time.time(),))
            async with connection.execute("UPDATE configuration_requests SET status='running' WHERE id="
                "(SELECT id FROM configuration_requests WHERE status='queued' AND self_id="
                "CAST((SELECT value FROM runtime_state WHERE key='onebot_self_id') AS INTEGER) ORDER BY id LIMIT 1) "
                "AND status='queued' RETURNING *") as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def failed(self, request: dict, error: str) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE configuration_requests SET status='failed',error=? WHERE id=?", (error, request["id"]))
            await enqueue_text(connection, "配置解析失败：" + error + "。可使用 /config <群号> mode inbox 等明确命令。", request["self_id"])

    async def retry(self, identifier: int) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE configuration_requests SET retry_count=retry_count+1 WHERE id=?", (identifier,))
