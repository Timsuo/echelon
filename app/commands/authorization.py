import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.commands.policies import PolicyCommands
from app.onebot.groups import GroupVerifier
from app.policies.models import GroupPolicy
from app.storage.history_repository import rows
from app.storage.policy_repository import PolicyRepository, read_policy
from app.storage.repository import Repository
from app.storage.request_identity import claim_command, request_exists, request_running


class AuthorizationChange(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operation: Literal['add', 'remove']
    group_id: int = Field(gt=0)
    group_name: str | None
    before_active: bool
    before_updated_at: float | None
    policy: GroupPolicy


async def propose_authorization(policies, self_id, admin_qq, group_id, operation, name=None, request_id=None, message_id=None):
    async with policies.db.transaction() as connection:
        await policies._account(connection, self_id)
        if not await request_running(connection, self_id, admin_qq, 'group_authorization', request_id) or not await claim_command(connection, self_id, message_id):
            return None
        known = await rows(connection, 'SELECT * FROM group_authorizations WHERE self_id=? AND group_id=?', (self_id, group_id))
        current = known[0] if known else {}
        active = bool(current.get('active'))
        if active == (operation == 'add'):
            raise ValueError('该群已经处于目标授权状态。')
        policy = await read_policy(connection, self_id, group_id)
        change = AuthorizationChange(operation=operation, group_id=group_id, group_name=name or current.get('group_name'),
            before_active=active, before_updated_at=current.get('updated_at'), policy=policy.model_dump())
        now = time.time()
        identifier = (await rows(connection, 'INSERT INTO configuration_proposals(self_id,admin_qq,kind,intent_json,created_at,expires_at,source_request_id) '
            "VALUES (?,?,'group_authorization',?,?,?,?) RETURNING id", (self_id, admin_qq, change.model_dump_json(), now, now+600, request_id)))[0]['id']
        text = [f'【Echelon 群授权变更】\n{operation.upper()} · {group_id}', change.group_name or str(group_id),
                f'授权：{active} → {operation == "add"}']
        if operation == 'add':
            text.extend(['确认后开始接收该群消息。', f'{"恢复现有" if current else "初始"} Policy：{policy.mode.upper()}',
                         f'Summary：{policy.summary_enabled}；Inbox：{policy.inbox_enabled}',
                         f'Priority Watch：{policy.priority_watch_enabled}；下载：{policy.attachment_download_enabled}'])
        else:
            text.append('停止新消息采集、历史核验、Triage、新附件下载和新的自动提醒。\n已有消息、Inbox、总结和已下载附件不会删除。')
        text.extend(['10分钟内确认：', f'/confirm {identifier}', f'/cancel {identifier}'])
        await Repository.enqueue_text(connection, '\n'.join(text), self_id)
        if request_id is not None:
            await connection.execute("UPDATE configuration_requests SET status='completed' WHERE id=? AND self_id=?", (request_id, self_id))
        return identifier


class AuthorizationCommands(PolicyCommands):
    def __init__(self, repository, admin_qq):
        super().__init__(repository, None, admin_qq)

    async def allow(self, event, argument):
        await self.account(event)
        if not argument:
            lines = ['【Echelon Group Authorization】', '正在接收：']
            groups = await self.repository.authorizations.list_known(event.self_id)
            for active in (True, False):
                if not active:
                    lines.append('\n已停用但保留历史：')
                for group in groups:
                    if bool(group['active']) != active:
                        continue
                    async with self.repository.db.transaction() as connection:
                        policy = await read_policy(connection, event.self_id, group['group_id'])
                    lines.append(f"{group['group_id']} · {policy.alias or group['group_name'] or '未缓存群名'}\nPolicy：{policy.mode.upper()}")
            lines.append('\n/allow add <群号>\n/allow remove <群号>\n移除只停止未来采集，不删除已有历史。')
            await self.repository.notify('\n'.join(lines), event.self_id)
            return
        parts = argument.split()
        if len(parts) != 2 or parts[0] not in {'add', 'remove'}:
            raise ValueError('用法：/allow [add|remove <群号>]')
        group_id = self.identifier(parts[1])
        if parts[0] == 'remove':
            await propose_authorization(self.policies, event.self_id, self.admin_qq, group_id, 'remove', message_id=event.message_id)
        else:
            # Verification must not wait in the WebSocket receiver: it needs that
            # same receiver to deliver API responses. Use the existing work queue.
            async with self.repository.db.transaction() as connection:
                await self.policies._account(connection, event.self_id)
                if await request_exists(connection, event.self_id, event.message_id):
                    return
                count = await rows(connection, "SELECT count(*) AS n FROM configuration_requests WHERE status IN ('queued','running')")
                if count[0]['n'] >= 20:
                    raise ValueError('配置解析队列已满，请稍后再试')
                inserted = await connection.execute('INSERT INTO configuration_requests(self_id,admin_qq,group_id,target_group_id,message_id,input_text,created_at,kind) '
                    "VALUES (?,?,?,?,?,'add',?,'group_authorization') ON CONFLICT DO NOTHING",
                    (event.self_id, self.admin_qq, group_id, group_id, event.message_id, time.time()))
                if inserted.rowcount == 0:
                    return
                await Repository.enqueue_text(connection, '正在验证机器人群成员身份；完成后发送授权提案，确认前不生效。', event.self_id)


async def verify_request(policies: PolicyRepository, actions, request):
    if actions is None:
        raise ValueError('OneBot 连接不可用')
    name = await GroupVerifier(actions).verify(request['self_id'], request['target_group_id'])
    await propose_authorization(policies, request['self_id'], request['admin_qq'], request['target_group_id'], 'add', name, request_id=request['id'])
