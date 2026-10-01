import json
import logging
import time

from app.commands.router import CommandRouter
from app.config import AttachmentConfig
from app.onebot.adapter import parse_event
from app.onebot.files import FileResolver, file_references, upload_notice
from app.onebot.normalizer import normalize
from app.storage.policy_repository import PolicyRepository
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, repository: Repository, allowed: list[int], router: CommandRouter,
                 attachment_config: AttachmentConfig | None = None, resolver: FileResolver | None = None) -> None:
        self.repository = repository
        self.allowed = frozenset(allowed)
        self.router = router
        self.attachment_config = attachment_config or AttachmentConfig()
        self.resolver = resolver

    async def handle(self, payload: dict) -> None:
        event = upload_notice(payload) or parse_event(payload)
        if event is None:
            return
        if event.message_type == "private":
            await self.router.dispatch(event)
            return
        if event.group_id not in self.allowed:
            logger.info("Group filtered group_id=%s", event.group_id)
            return
        policy = await PolicyRepository(self.repository.db, list(self.allowed)).get(event.self_id, event.group_id)
        if policy.mode == "ignore":
            logger.info("Group ignored by policy group_id=%s", event.group_id)
            return
        normalized = normalize(event.message)
        record = {
            "self_id": event.self_id, "group_id": event.group_id,
            "message_id": event.message_id, "user_id": event.user_id,
            "nickname": event.sender.card or event.sender.nickname,
            "event_time": event.time, "received_time": time.time(),
            "raw_message": event.raw_message or (event.message if isinstance(event.message, str) else ""),
            "normalized_text": normalized.text, "reply_to_message_id": normalized.reply_to_message_id,
            "message_json": json.dumps(payload, ensure_ascii=False),
        }
        files = file_references(event, payload.get("post_type", "message")) if self.attachment_config.enabled else []
        inserted = await self.repository.add_message(record, files, self.attachment_config)
        if self.resolver:
            self.resolver.remember(files)
        logger.info("Group message group_id=%s inserted=%s", event.group_id, inserted)
