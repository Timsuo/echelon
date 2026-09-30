import json
import logging
import time

from app.commands.router import CommandRouter
from app.onebot.adapter import parse_event
from app.onebot.normalizer import normalize
from app.storage.repository import Repository

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, repository: Repository, allowed: list[int], router: CommandRouter) -> None:
        self.repository = repository
        self.allowed = frozenset(allowed)
        self.router = router

    async def handle(self, payload: dict) -> None:
        event = parse_event(payload)
        if event is None:
            return
        if event.message_type == "private":
            await self.router.dispatch(event)
            return
        if event.group_id not in self.allowed:
            logger.info("Group filtered group_id=%s", event.group_id)
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
        inserted = await self.repository.add_message(record)
        logger.info("Group message group_id=%s inserted=%s", event.group_id, inserted)
