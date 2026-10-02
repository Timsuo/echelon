"""NapCat group verification compatibility stays at the protocol boundary.

get_group_info can fetch public details for a non-member group. Membership must
also be proven by get_group_list; neither capability reads group messages.
"""
from pydantic import BaseModel, ConfigDict, Field

from app.onebot.adapter import parse_self_id


class GroupInfoQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    self_id: int = Field(gt=0)
    group_id: int = Field(gt=0)


class GroupListQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    self_id: int = Field(gt=0)


class GroupVerifier:
    def __init__(self, actions):
        self.actions = actions

    async def verify(self, self_id: int, group_id: int) -> str:
        groups = await self.actions.call('get_group_list', {'self_id': self_id})
        if not isinstance(groups, list) or not any(
            isinstance(group, dict) and parse_self_id(group.get('group_id')) == group_id for group in groups
        ):
            raise ValueError('无法确认机器人已加入该群，未生成授权提案。')
        info = await self.actions.call('get_group_info', {'self_id': self_id, 'group_id': group_id})
        if not isinstance(info, dict) or parse_self_id(info.get('group_id')) != group_id:
            raise ValueError('群资料不匹配')
        name = info.get('group_name')
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise ValueError('群名无法验证')
        return name
