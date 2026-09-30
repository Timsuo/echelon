"""OneBot/NapCat wire-format differences stay in this module and normalizer."""
import hmac
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def parse_self_id(value: Any) -> int:
    """Accept only positive decimal IDs representable by SQLite INTEGER."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Invalid self_id")
    text = str(value)
    if not re.fullmatch(r"[0-9]{1,19}", text) or not 0 < int(text) <= 2**63 - 1:
        raise ValueError("Invalid self_id")
    return int(text)


class Sender(BaseModel):
    nickname: str = ""
    card: str = ""

    @field_validator("nickname", "card", mode="before")
    @classmethod
    def null_name(cls, value: Any) -> Any:
        return "" if value is None else value


class MessageEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    post_type: Literal["message"]
    message_type: Literal["group", "private"]
    self_id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    group_id: int | None = Field(default=None, gt=0)
    message_id: str = Field(min_length=1, max_length=100)
    time: int = Field(ge=0, le=253402300799)
    message: str | list[dict[str, Any]]
    raw_message: str = ""
    sender: Sender = Field(default_factory=Sender)

    @field_validator("message_id", mode="before")
    @classmethod
    def string_id(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def parse_event(payload: dict[str, Any]) -> MessageEvent | None:
    if payload.get("post_type") != "message" or payload.get("message_type") not in {"group", "private"}:
        return None
    data = dict(payload)
    if "message" not in data:
        data["message"] = data.get("raw_message", "")
    event = MessageEvent.model_validate(data)
    if event.message_type == "group" and event.group_id is None:
        raise ValueError("Group event without group_id")
    return event


def authenticated(headers: Any, query: Any, expected: str) -> bool:
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    candidate = token if scheme.lower() in {"bearer", "token"} else query.get("access_token", "")
    return bool(expected and candidate) and hmac.compare_digest(candidate.encode(), expected.encode())


def response_echo(payload: dict[str, Any]) -> str | None:
    if "post_type" not in payload and "echo" in payload:
        echo = payload["echo"]
        return echo if isinstance(echo, str) else None
    return None
