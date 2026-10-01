"""NapCat recent-history compatibility boundary. No cursor/pagination assumptions escape here."""
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.onebot.adapter import MessageEvent, parse_self_id

if TYPE_CHECKING:
    from app.onebot.actions import ActionGateway

logger = logging.getLogger(__name__)


class GroupHistoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    self_id: int = Field(gt=0, le=2**63 - 1)
    group_id: int = Field(gt=0, le=2**63 - 1)
    count: int = Field(ge=1, le=500)

    def wire(self) -> dict:
        # Current NapCat supports reverse_order and legacy reverseOrder. No cursor means latest N.
        return {"group_id": str(self.group_id), "count": self.count, "reverseOrder": False}


class HistorySchemaError(ValueError):
    pass


class HistoryIdentityError(PermissionError):
    pass


@dataclass
class HistoryBatch:
    messages: list[dict]
    received: int
    invalid: int


def parse_history(data: object, query: GroupHistoryQuery, now: float) -> HistoryBatch:
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list) or len(data["messages"]) > 500:
        raise HistorySchemaError("Invalid history response envelope")
    messages, invalid = [], 0
    for item in data["messages"]:
        try:
            if not isinstance(item, dict):
                raise ValueError("Invalid history item")
            if ("self_id" in item and parse_self_id(item["self_id"]) != query.self_id):
                raise HistoryIdentityError("History self_id mismatch")
            if "group_id" in item and parse_self_id(item["group_id"]) != query.group_id:
                raise HistoryIdentityError("History group_id mismatch")
            if item.get("message_type", "group") != "group" or item.get("post_type", "message") != "message":
                raise ValueError("Not a group message")
            # Missing self_id can be supplied only from the authenticated, account-scoped action.
            normalized = {**item, "self_id": query.self_id, "post_type": "message", "message_type": "group"}
            normalized["group_id"] = parse_self_id(item["group_id"])
            normalized["user_id"] = parse_self_id(item["user_id"])
            if type(item.get("time")) is not int or not 0 < item["time"] <= now + 300:
                raise ValueError("Missing/invalid event time")
            sender = item.get("sender")
            if not isinstance(sender, dict):
                raise ValueError("Missing sender")
            if "user_id" in sender and parse_self_id(sender["user_id"]) != normalized["user_id"]:
                raise ValueError("Sender mismatch")
            for name in ("nickname", "card"):
                if name in sender and sender[name] is not None and not isinstance(sender[name], str):
                    raise ValueError("Invalid sender name")
            message = item.get("message")
            if isinstance(message, list):
                if any(not isinstance(segment, dict) or not isinstance(segment.get("type"), str)
                       or not isinstance(segment.get("data"), dict) for segment in message):
                    raise ValueError("Invalid segments")
            elif not isinstance(message, str):
                raise ValueError("Missing message")
            MessageEvent.model_validate(normalized)
            messages.append(normalized)
        except HistoryIdentityError:
            raise
        except (ValidationError, ValueError, KeyError, TypeError):
            invalid += 1
            logger.warning("Invalid history item skipped group_id=%s", query.group_id)
    return HistoryBatch(messages, len(data["messages"]), invalid)


class HistoryAdapter:
    def __init__(self, actions: "ActionGateway") -> None:
        self.actions = actions

    async def fetch(self, self_id: int, group_id: int, count: int, now: float) -> HistoryBatch:
        query = GroupHistoryQuery(self_id=self_id, group_id=group_id, count=count)
        data = await self.actions.call("get_group_msg_history", query.model_dump())
        return parse_history(data, query, now)
