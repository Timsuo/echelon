import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from app.attachments.storage import AttachmentStorage
from app.commands.authorization import AuthorizationCommands
from app.commands.delivery import DeliveryCommands
from app.commands.help import COMMANDS, render_help
from app.commands.history import HistoryCommands
from app.commands.inbox import InboxCommands
from app.commands.operations import OperationalCommands
from app.commands.policies import PolicyCommands
from app.commands.preferences import PreferenceCommands
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
        policies = PolicyCommands(repository, None, admin_qq)
        history = HistoryCommands(repository, config, actions)
        preferences = PreferenceCommands(repository, None, admin_qq)
        authorization = AuthorizationCommands(repository, admin_qq)
        delivery = DeliveryCommands(repository, config, admin_qq)
        operations = OperationalCommands(repository, actions, config)
        handlers: dict[str, CommandHandler] = {
            "/doctor": operations.doctor, "/outbox": operations.outbox,
            "/allow": authorization.allow, "/delivery": delivery.delivery, "/notify": delivery.notify, "/digest": delivery.digest,
            "/help": self.help, "/coverage": history.coverage, "/sync": history.sync,
            "/status": self.status, "/summary": self.summary,
            "/inbox": inbox.listing, "/detail": inbox.detail, "/archive": inbox.archive, "/file": inbox.file,
            "/groups": policies.groups, "/group": policies.group, "/config": policies.config,
            "/confirm": policies.confirm, "/cancel": policies.cancel,
            "/prefs": preferences.prefs, "/pref": preferences.pref,
        }
        self.handlers = {}
        for spec in COMMANDS:
            handler = handlers['/' + spec.name]
            for name in (spec.name, *spec.aliases):
                self.handlers['/' + name] = handler

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
            await self.repository.notify("未知命令。\n\n发送 /help 查看 Echelon 支持的命令。", event.self_id)
            return
        if name in {'/summary', '/digest', '/config', '/pref', '/notify', '/allow', '/confirm', '/cancel'}:
            seen = await self.repository.query('SELECT 1 FROM command_receipts WHERE self_id=? AND message_id=? '
                'UNION ALL SELECT 1 FROM configuration_requests WHERE self_id=? AND message_id=? LIMIT 1',
                (event.self_id, event.message_id, event.self_id, event.message_id))
            if seen:
                return
        try:
            await handler(event, argument)
        except ValueError as error:
            # Only our own argument validation reaches here, never raw LLM/event payloads.
            await self.repository.notify(str(error), event.self_id)

    async def help(self, event: MessageEvent, argument: str) -> None:
        await self.repository.notify(render_help(argument), event.self_id)

    async def status(self, event: MessageEvent, argument: str) -> None:
        if argument:
            raise ValueError("用法：/status")
        await self.repository.notify(await render_status(
            self.repository, self.actions, self.config, self.started_at), event.self_id)

    async def summary(self, event: MessageEvent, argument: str) -> None:
        from app.commands.saved_summaries import SavedSummaryCommands
        if await SavedSummaryCommands(self.repository, self.config).handle(event, argument):
            return
        start, end = parse_window(argument, datetime.now(UTC), self.config.timezone)
        policies = await PolicyRepository(self.repository.db).listing(event.self_id)
        groups = [p.group_id for p in policies if p.mode != "ignore" and p.summary_enabled]
        jobs = await self.repository.queue_summaries(event.self_id, event.message_id, groups, start, end)
        logger.info("Summary jobs queued ids=%s", jobs)
