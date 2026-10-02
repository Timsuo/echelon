
from pydantic import ValidationError

from app.onebot.adapter import MessageEvent
from app.policies.models import GroupPolicy
from app.policies.parser import ConfigIntentParser
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository


def describe(policy: GroupPolicy) -> str:
    return (f"# {policy.group_id}\n别名：{policy.alias or '未设置'}\nMode：{policy.mode.upper()}\n"
            f"Summary：{policy.summary_enabled}\nInbox：{policy.inbox_enabled}\n"
            f"Attachment Download：{policy.attachment_download_enabled}\n"
            f"Priority Watch：{policy.priority_watch_enabled}")


class PolicyCommands:
    def __init__(self, repository: Repository, allowed: list[int], admin_qq: int) -> None:
        self.repository = repository
        self.policies = PolicyRepository(repository.db, allowed)
        self.admin_qq = admin_qq

    async def account(self, event: MessageEvent) -> None:
        if event.user_id != self.admin_qq or await self.repository.state("onebot_self_id") != str(event.self_id):
            raise ValueError("配置命令账号不匹配")

    async def groups(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        if argument:
            raise ValueError("用法：/groups")
        rows = await self.policies.listing(event.self_id)
        await self.repository.notify("【Echelon Groups】\n\n" + "\n\n".join(map(describe, rows)) +
            "\n\n/group <群号>\n/config <群号或别名> ...\nPriority Watch 配合 /delivery 控制即时提醒。", event.self_id)

    @staticmethod
    def identifier(argument: str) -> int:
        if not argument.isascii() or not argument.isdecimal() or not 0 < len(argument) <= 18 or int(argument) <= 0:
            raise ValueError("请提供一个有效的数字 ID")
        return int(argument)

    async def group(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        policy = await self.policies.get(event.self_id, self.identifier(argument))
        await self.repository.notify("【Group Policy】\n" + describe(policy), event.self_id)

    async def config(self, event: MessageEvent, argument: str) -> None:
        await self.account(event)
        if not argument or len(argument) > 2000:
            raise ValueError("用法：/config <群号或别名> <配置意图>（最多2000字）")
        policies = await self.policies.listing(event.self_id)
        group_id = ConfigIntentParser.target(argument, policies)
        current = next(policy for policy in policies if policy.group_id == group_id)
        try:
            intent = ConfigIntentParser.local(argument, group_id, current.alias)
        except ValidationError as error:
            raise ValueError("配置字段格式不合法") from error
        if intent:
            await self.policies.propose(event.self_id, self.admin_qq, group_id, intent)
        else:
            await self.policies.queue(event.self_id, self.admin_qq, group_id, event.message_id, argument)

    async def confirm(self, event: MessageEvent, argument: str) -> None:
        await self.finish(event, argument, True)

    async def cancel(self, event: MessageEvent, argument: str) -> None:
        await self.finish(event, argument, False)

    async def finish(self, event: MessageEvent, argument: str, confirm: bool) -> None:
        await self.account(event)
        text = await self.policies.resolve(event.self_id, self.admin_qq, self.identifier(argument), confirm)
        await self.repository.notify(text, event.self_id)
