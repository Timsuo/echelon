"""One atomic confirmation boundary for all four typed proposal handlers."""
import json
import time

from app.policies.models import POLICY_FIELDS, GroupPolicy
from app.storage.authorization_repository import activate, deactivate, is_active
from app.storage.history_repository import rows
from app.storage.policy_repository import read_policy, save_policy
from app.storage.preference_repository import apply_preference_proposal
from app.storage.repository import Repository
from app.storage.request_identity import claim_command


async def apply_policy(connection, self_id, data):
    group_id = data['group_id']
    if not await is_active(connection, self_id, group_id):
        raise ValueError('该群当前未授权采集。')
    current = await read_policy(connection, self_id, group_id)
    if set(data['before']) != set(data['after']) or not set(data['after']) <= set(POLICY_FIELDS):
        raise ValueError('Invalid policy proposal')
    if any(getattr(current, key) != value for key, value in data['before'].items()):
        return False
    await save_policy(connection, GroupPolicy.model_validate(current.model_dump() | data['after']))
    return True


async def apply_authorization(connection, self_id, data):
    from app.commands.authorization import AuthorizationChange
    change = AuthorizationChange.model_validate(data)
    known = await rows(connection, 'SELECT * FROM group_authorizations WHERE self_id=? AND group_id=?',
                       (self_id, change.group_id))
    version = known[0]['updated_at'] if known else None
    if version != change.before_updated_at or bool(known and known[0]['active']) != change.before_active:
        return False
    policy = await read_policy(connection, self_id, change.group_id)
    if policy != change.policy:
        return False
    if change.operation == 'add':
        await activate(connection, self_id, change.group_id, change.group_name)
        await save_policy(connection, policy)
    else:
        await deactivate(connection, self_id, change.group_id)
    return True


async def apply_delivery(connection, self_id, data):
    from app.delivery.preferences import apply_delivery_proposal
    return await apply_delivery_proposal(connection, self_id, data)


HANDLERS = {'group_policy': apply_policy, 'triage_preferences': apply_preference_proposal,
            'group_authorization': apply_authorization, 'delivery_preferences': apply_delivery}


class ConfigurationProposalService:
    def __init__(self, policies):
        self.policies = policies

    async def resolve(self, self_id, admin_qq, identifier, confirm, message_id=None):
        async with self.policies.db.transaction() as connection:
            await self.policies._account(connection, self_id)
            if not await claim_command(connection, self_id, message_id):
                return ''
            text = await self.resolve_on(connection, self_id, admin_qq, identifier, confirm)
            if message_id is not None:
                await Repository.enqueue_text(connection, text, self_id)
            self.policies.db.delivery_wakeup.set()
            return text

    async def resolve_on(self, connection, self_id, admin_qq, identifier, confirm):
        found = await rows(connection, 'SELECT * FROM configuration_proposals WHERE id=? AND self_id=? AND admin_qq=?',
                           (identifier, self_id, admin_qq))
        if not found:
            raise ValueError('配置提案不存在')
        proposal = found[0]
        if proposal['status'] != 'pending':
            return '该提案已处理：' + proposal['status']
        if proposal['expires_at'] <= time.time():
            await connection.execute("UPDATE configuration_proposals SET status='expired' WHERE id=?", (identifier,))
            return '配置提案已过期，请重新提交。'
        if confirm:
            handler = HANDLERS.get(proposal['kind'])
            if handler is None:
                raise ValueError('未知配置提案类型')
            if not await handler(connection, self_id, json.loads(proposal['intent_json'])):
                await connection.execute("UPDATE configuration_proposals SET status='expired' WHERE id=?", (identifier,))
                return '配置已发生变化，该提案已失效，请重新提交。'
        await connection.execute('UPDATE configuration_proposals SET status=? WHERE id=?',
                                 ('confirmed' if confirm else 'cancelled', identifier))
        return '配置已更新。' if confirm else '配置提案已取消。'
