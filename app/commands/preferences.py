import json

from app.commands.policies import PolicyCommands
from app.onebot.adapter import MessageEvent
from app.storage.preference_repository import PreferenceRepository


class PreferenceCommands(PolicyCommands):
    async def prefs(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        if argument:
            raise ValueError('用法：/prefs')
        prefs = await PreferenceRepository(self.policies).get(event.self_id)
        lines = ['【Echelon Preferences】', '重要关键词：' + ('、'.join(prefs.important_keywords) or '无'),
                 '低优先级关键词：' + ('、'.join(prefs.low_priority_keywords) or '无'),
                 '重要发送者：' + ('、'.join(map(str, prefs.important_senders)) or '无'),
                 '分类偏好：' + json.dumps(prefs.category_priority_preferences, ensure_ascii=False),
                 '修改：/pref <自然语言> → /confirm <id>；仅影响后续分类，不发送即时提醒。']
        await self.repository.notify('\n'.join(lines), event.self_id)

    async def pref(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        if not argument or len(argument) > 2000:
            raise ValueError('用法：/pref <个人偏好>（最多2000字）')
        await PreferenceRepository(self.policies).queue(event.self_id, self.admin_qq, event.message_id, argument)
