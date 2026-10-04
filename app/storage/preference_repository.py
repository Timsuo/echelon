import json
import logging
import re
import time

from app.storage.history_repository import rows
from app.storage.repository import Repository
from app.storage.request_identity import request_exists, request_running
from app.triage.models import PreferenceIntent, TriagePreferences

logger = logging.getLogger(__name__)


async def read_preferences(connection, self_id: int) -> TriagePreferences:
    found = await rows(connection, "SELECT preferences_json FROM triage_preferences WHERE self_id=?", (self_id,))
    return TriagePreferences.model_validate_json(found[0]['preferences_json']) if found else TriagePreferences()


def validate_preference_intent(intent: PreferenceIntent, text: str) -> None:
    changes = intent.changes
    cues = {'high': ('重要', '重点', '关注', '高优先', '优先看', 'high', 'important'),
            'low': ('低优先', '不重要', '不太重要', '不关注', 'low'),
            'normal': ('正常', '普通', '一般', 'normal'),
            'critical': ('紧急', '极其重要', 'critical')}
    removing = any(cue in text for cue in ('删除', '移除', '取消', '不再', 'remove', '清除'))
    for field in ('important_keywords', 'low_priority_keywords', 'important_senders'):
        if getattr(changes, field + '_remove') and not removing:
            raise ValueError('Preference removal not explicitly requested')
        level = 'low' if field == 'low_priority_keywords' else 'high'
        if getattr(changes, field + '_add') and not any(cue in text for cue in cues[level]):
            raise ValueError('Preference importance not explicitly requested')
    for field in ('important_keywords_add', 'important_keywords_remove', 'low_priority_keywords_add', 'low_priority_keywords_remove'):
        if any(word not in text for word in getattr(changes, field)):
            raise ValueError("Preference keyword not explicitly mentioned")
    ids = {int(value) for value in re.findall(r'(?<![0-9])[0-9]{1,19}(?![0-9])', text)}
    if not set(changes.important_senders_add + changes.important_senders_remove) <= ids:
        raise ValueError("Preference sender not explicitly mentioned")
    aliases = {'announcement': ('通知',), 'schedule': ('调课', '时间安排'), 'assignment': ('作业', '报告'),
        'exam': ('考试',), 'material': ('课程资料', '课件', '材料'), 'administrative': ('行政',),
        'action_request': ('行动请求',), 'project': ('项目',), 'discussion': ('讨论',), 'other': ('其他',)}
    for category, priority in changes.category_priority_preferences.items():
        if not any(term in text for term in (category, *aliases[category])):
            raise ValueError("Preference category not explicitly mentioned")
        if (priority is None and not removing) or (priority is not None and not any(cue in text for cue in cues[priority])):
            raise ValueError('Preference category priority not explicitly requested')


async def apply_preference_proposal(connection, self_id: int, data: dict) -> bool:
    current = await read_preferences(connection, self_id)
    before, after = data['before'], data['after']
    if set(before) != set(after) or not set(after) <= set(TriagePreferences.model_fields):
        raise ValueError("Invalid preference proposal fields")
    if any(current.model_dump()[key] != value for key, value in before.items()):
        return False
    updated = TriagePreferences.model_validate(current.model_dump() | after)
    await connection.execute("INSERT INTO triage_preferences VALUES (?,?,?) ON CONFLICT(self_id) DO UPDATE SET "
        "preferences_json=excluded.preferences_json,updated_at=excluded.updated_at", (self_id, updated.model_dump_json(), time.time()))
    return True


class PreferenceRepository:
    def __init__(self, policies) -> None:
        self.policies, self.db = policies, policies.db

    async def get(self, self_id: int) -> TriagePreferences:
        async with self.db.transaction() as connection:
            await self.policies._account(connection, self_id)
            return await read_preferences(connection, self_id)

    async def queue(self, self_id: int, admin_qq: int, message_id: str, text: str) -> None:
        async with self.db.transaction() as connection:
            await self.policies._account(connection, self_id)
            if await request_exists(connection, self_id, message_id):
                return
            count = await rows(connection, "SELECT count(*) AS n FROM configuration_requests WHERE status IN ('queued','running')")
            if count[0]['n'] >= 20:
                raise ValueError("配置解析队列已满，请稍后再试")
            inserted = await connection.execute("INSERT INTO configuration_requests(self_id,admin_qq,group_id,message_id,input_text,created_at,kind) "
                "VALUES (?,?,0,?,?,?,'triage_preferences') ON CONFLICT DO NOTHING", (self_id, admin_qq, message_id, text, time.time()))
            if inserted.rowcount == 0:
                return
            await Repository.enqueue_text(connection, "正在解析个人偏好；仅生成提案，/confirm 后才生效。", self_id)

    async def propose(self, request: dict, intent: PreferenceIntent) -> int | None:
        validate_preference_intent(intent, request['input_text'])
        async with self.db.transaction() as connection:
            await self.policies._account(connection, request['self_id'])
            if not await request_running(connection, request['self_id'], request['admin_qq'], 'triage_preferences', request['id']):
                return None
            current = await read_preferences(connection, request['self_id'])
            updated = intent.changes.apply(current)
            after = {key: value for key, value in updated.model_dump().items() if current.model_dump()[key] != value}
            identifier = None
            if after:
                before = {key: current.model_dump()[key] for key in after}
                data = dict(before=before, after=after)
                identifier = (await rows(connection, "INSERT INTO configuration_proposals(self_id,admin_qq,intent_json,created_at,expires_at,kind,source_request_id) "
                    "VALUES (?,?,?,?,?,'triage_preferences',?) RETURNING id", (request['self_id'], request['admin_qq'],
                    json.dumps(data, ensure_ascii=False), time.time(), time.time() + 600, request['id'])))[0]['id']
                lines = ['【Echelon 偏好变更】']
                for key, value in after.items():
                    lines.append(f"{key}: {json.dumps(before[key], ensure_ascii=False)} → {json.dumps(value, ensure_ascii=False)}")
                lines.extend(['仅影响后续分类，不重算旧 Inbox。10分钟内确认：', f'/confirm {identifier}', f'/cancel {identifier}'])
                await Repository.enqueue_text(connection, '\n'.join(lines), request['self_id'])
                logger.info("Preference proposal created id=%s", identifier)
            else:
                await Repository.enqueue_text(connection, '偏好已经符合要求，无需变更。', request['self_id'])
            await connection.execute("UPDATE configuration_requests SET status='completed' WHERE id=? AND self_id=? AND kind='triage_preferences'",
                                     (request['id'], request['self_id']))
            return identifier
