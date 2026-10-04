import json
import logging
import time

from app.policies.models import POLICY_FIELDS, ConfigIntent, GroupPolicy
from app.storage.authorization_repository import active_ids, is_active
from app.storage.db import Database
from app.storage.repository import Repository
from app.storage.request_identity import claim_command, request_exists, request_running

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
        lines.append("Priority Watch 开启后，符合投递偏好的 HIGH / CRITICAL 可自动提醒。")
    return "\n".join(lines)


class PolicyRepository:
    def __init__(self, db: Database, allowed=None) -> None:
        self.db = db

    async def check_group(self, self_id: int, group_id: int, connection=None) -> None:
        if connection is None:
            async with self.db.transaction() as connection:
                return await self.check_group(self_id, group_id, connection)
        if not await is_active(connection, self_id, group_id):
            raise ValueError("该群当前未授权采集。请先 /allow add <群号> 并确认。")

    async def get(self, self_id: int, group_id: int) -> GroupPolicy:
        async with self.db.transaction() as connection:
            await self.check_group(self_id, group_id, connection)
            return await read_policy(connection, self_id, group_id)

    async def listing(self, self_id: int) -> list[GroupPolicy]:
        async with self.db.transaction() as connection:
            return [await read_policy(connection, self_id, group_id) for group_id in await active_ids(connection, self_id)]

    async def _account(self, connection, self_id: int) -> None:
        async with connection.execute("SELECT value FROM runtime_state WHERE key='onebot_self_id'") as cursor:
            row = await cursor.fetchone()
        if row is None or row[0] != str(self_id):
            raise ValueError("机器人账号不匹配，未应用配置")

    async def propose(self, self_id: int, admin_qq: int, group_id: int, intent: ConfigIntent,
                      request_id: int | None = None, message_id: str | None = None) -> int | None:
        async with self.db.transaction() as connection:
            await self._account(connection, self_id)
            if not await request_running(connection, self_id, admin_qq, 'group_policy', request_id) or not await claim_command(connection, self_id, message_id):
                return None
            await self.check_group(self_id, group_id, connection)
            current = await read_policy(connection, self_id, group_id)
            after = {key: value for key, value in intent.changes.expanded().items()
                     if getattr(current, key) != value}
            if after:
                data = dict(group_id=group_id, before={key: getattr(current, key) for key in after}, after=after)
                now = time.time()
                async with connection.execute("INSERT INTO configuration_proposals"
                    "(self_id,admin_qq,intent_json,created_at,expires_at,source_request_id) VALUES (?,?,?,?,?,?)",
                    (self_id, admin_qq, json.dumps(data, ensure_ascii=False), now, now + 600, request_id)) as cursor:
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

    async def resolve(self, self_id: int, admin_qq: int, identifier: int, confirm: bool, message_id=None) -> str:
        from app.policies.proposals import ConfigurationProposalService
        return await ConfigurationProposalService(self).resolve(self_id, admin_qq, identifier, confirm, message_id)

    async def queue(self, self_id: int, admin_qq: int, group_id: int, message_id: str, text: str) -> None:
        async with self.db.transaction() as connection:
            await self._account(connection, self_id)
            if await request_exists(connection, self_id, message_id):
                return
            await self.check_group(self_id, group_id, connection)
            async with connection.execute("SELECT count(*) FROM configuration_requests WHERE status IN ('queued','running')") as cursor:
                if (await cursor.fetchone())[0] >= 20:
                    raise ValueError("配置解析队列已满，请稍后再试")
            inserted = await connection.execute("INSERT INTO configuration_requests(self_id,admin_qq,group_id,message_id,input_text,created_at,target_group_id) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", (self_id, admin_qq, group_id, message_id, text, time.time(), group_id))
            if inserted.rowcount == 0:
                return
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
            async with connection.execute("UPDATE configuration_requests SET status='failed',error=? WHERE id=? AND self_id=? AND status='running' RETURNING id", (error, request["id"], request['self_id'])) as cursor:
                if await cursor.fetchone() is None:
                    return
            hint = {'triage_preferences': '请用 /pref 明确说明偏好后重试。',
                    'delivery_preferences': '请用 /notify 明确说明投递时间或开关后重试。',
                    'group_authorization': '请确认机器人已加入该群且 OneBot 在线，再用 /allow add 重试。'}.get(
                        request.get('kind'), '可使用 /config <群号> mode inbox 等明确命令。')
            await enqueue_text(connection, "配置解析失败：" + error + "。" + hint, request["self_id"])

    async def retry(self, identifier: int) -> None:
        async with self.db.transaction() as connection:
            await connection.execute("UPDATE configuration_requests SET retry_count=retry_count+1 WHERE id=?", (identifier,))
