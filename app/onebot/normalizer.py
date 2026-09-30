import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedMessage:
    text: str
    reply_to_message_id: str | None


def unescape(value: str) -> str:
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def cq_segments(message: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    offset = 0
    for match in re.finditer(r"\[CQ:([\w-]+)((?:,[^\]]*)?)\]", message):
        if match.start() > offset:
            segments.append({"type": "text", "data": {"text": unescape(message[offset:match.start()])}})
        data = {}
        for item in match.group(2).lstrip(",").split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                data[key] = unescape(value)
        segments.append({"type": match.group(1), "data": data})
        offset = match.end()
    if offset < len(message):
        segments.append({"type": "text", "data": {"text": unescape(message[offset:])}})
    return segments


def normalize(message: str | list[dict[str, Any]]) -> NormalizedMessage:
    segments = cq_segments(message) if isinstance(message, str) else message
    parts = []
    reply = None
    for segment in segments:
        kind = segment.get("type", "unknown")
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            parts.append("[unsupported:malformed]")
            continue
        match kind:
            case "text":
                parts.append(str(data.get("text", "")))
            case "at":
                parts.append(f"[@{data.get('qq', '?')}]")
            case "reply":
                identifier = str(data.get("id", "?"))
                reply = reply or identifier
                parts.append(f"[回复 message_id={identifier}]")
            case "image":
                parts.append("[图片]")
            case "file":
                parts.append(f"[文件: {data.get('name') or data.get('file') or 'unknown'}]")
            case _:
                parts.append(f"[unsupported:{kind}]")
    return NormalizedMessage("".join(parts), reply)
