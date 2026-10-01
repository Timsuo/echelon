import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.attachments.storage import AttachmentStorage
from app.commands.inbox import InboxCommands
from app.commands.policies import PolicyCommands
from app.commands.status import render_status
from app.commands.summary import parse_window
from app.config import AppConfig
from app.onebot.actions import ActionGateway
from app.onebot.adapter import MessageEvent
from app.onebot.normalizer import normalize
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository

logger = logging.getLogger(__name__)
CommandHandler = Callable[[MessageEvent, str], Awaitable[None]]


class CommandRouter:
    def __init__(self, repository: Repository, config: AppConfig, admin_qq: int,
                 actions: ActionGateway, started_at: float, storage: AttachmentStorage | None = None) -> None:
        self.repository = repository
        self.config = config
        self.admin_qq = admin_qq
        self.actions = actions
        self.started_at = started_at
        inbox = InboxCommands(repository, config, storage)
        policies = PolicyCommands(repository, config.groups.allowed, admin_qq)
        self.handlers: dict[str, CommandHandler] = {
            "/status": self.status, "/summary": self.summary,
            "/inbox": inbox.listing, "/detail": inbox.detail, "/archive": inbox.archive, "/file": inbox.file,
            "/groups": policies.groups, "/group": policies.group, "/config": policies.config,
            "/confirm": policies.confirm, "/cancel": policies.cancel,
        }

    async def dispatch(self, event: MessageEvent) -> None:
        if event.user_id != self.admin_qq:
            logger.warning("Security: ignored unauthorized private event user_id=%s", event.user_id)
            return
        text = normalize(event.message).text.strip()
        if not text.startswith("/"):
            return
        parts = text.split(maxsplit=1)
        name = parts[0]
        argument = parts[1].strip() if len(parts) > 1 else ""
        handler = self.handlers.get(name)
        logger.info("Admin command recognized=%s", handler is not None)
        if handler is None:
            await self.repository.notify("支持：/status、/summary 2h、/summary 30m、/summary today、"
                                         "/inbox、/inbox unread、/detail <id>、/archive <id>、/file <id> <序号>、"
                                         "/groups、/group <群号>、/config ...、/confirm <id>、/cancel <id>")
            return
        try:
            await handler(event, argument)
        except ValueError as error:
            # Only our own argument validation reaches here, never raw LLM/event payloads.
            await self.repository.notify(str(error))

    async def status(self, event: MessageEvent, argument: str) -> None:
        if argument:
            raise ValueError("用法：/status")
        await self.repository.notify(await render_status(
            self.repository, self.actions, self.config, self.started_at))

    async def summary(self, event: MessageEvent, argument: str) -> None:
        start, end = parse_window(argument, datetime.now(UTC), self.config.timezone)
        if not self.config.groups.allowed:
            raise ValueError("尚未配置监听群，请修改 config/config.yaml 并重启")
        policies = await PolicyRepository(self.repository.db, self.config.groups.allowed).listing(event.self_id)
        groups = [p.group_id for p in policies if p.mode != "ignore" and p.summary_enabled]
        if not groups:
            raise ValueError("当前没有启用总结的群，请查看 /groups")
        jobs = await self.repository.queue_summaries(event.self_id, event.message_id, groups, start, end)
        logger.info("Summary jobs queued ids=%s", jobs)
