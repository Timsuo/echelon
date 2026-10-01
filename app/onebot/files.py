"""NapCat file segments and group_upload notices mapped to stable references."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
from copy import deepcopy
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.attachments.models import GroupFileReference
from app.onebot.adapter import MessageEvent
from app.onebot.normalizer import cq_segments

if TYPE_CHECKING:
    from app.onebot.actions import ActionGateway

logger = logging.getLogger(__name__)


def without_file_urls(payload: dict) -> dict:
    """Persist file provenance, not expiring download credentials embedded in file segments."""
    result = deepcopy(payload)
    for key in ("message", "raw_message"):
        value = result.get(key)
        if isinstance(value, list):
            for segment in value:
                if isinstance(segment, dict) and segment.get("type") == "file" and isinstance(segment.get("data"), dict):
                    segment["data"].pop("url", None)
        elif isinstance(value, str):
            result[key] = re.sub(r"\[CQ:file(?:,[^\]]*)?\]",
                lambda match: re.sub(r",url=[^,\]]*", "", match[0]), value)
    if result.get("notice_type") == "group_upload" and isinstance(result.get("file"), dict):
        result["file"].pop("url", None)
    return result


def upload_notice(payload: dict) -> MessageEvent | None:
    if payload.get("post_type") != "notice" or payload.get("notice_type") != "group_upload":
        return None
    file = payload.get("file")
    if not isinstance(file, dict):
        logger.warning("Invalid group_upload metadata")
        return None
    # Notices have no OneBot message_id. A namespaced deterministic ID keeps provenance.
    identity = [payload.get(key) for key in ("self_id", "group_id", "user_id", "time")]
    identity.append({key: file.get(key) for key in ("id", "name", "size", "busid")})
    synthetic = "notice:group_upload:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        return MessageEvent.model_validate({**payload, "post_type": "message", "message_type": "group",
            "message_id": synthetic, "message": [{"type": "file", "data": file}]})
    except ValidationError:
        logger.warning("Invalid group_upload event")
        return None


def file_references(event: MessageEvent, source_type: str) -> list[GroupFileReference]:
    segments = cq_segments(event.message) if isinstance(event.message, str) else event.message
    references = []
    for index, segment in enumerate(segments):
        if segment.get("type") != "file":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            logger.warning("Invalid file metadata")
            continue
        try:
            file_id = data.get("file_id") or data.get("id") or None
            if file_id is not None:
                if not isinstance(file_id, (str, int)) or isinstance(file_id, bool):
                    raise ValueError("Invalid file ID")
                file_id = str(file_id)
            key = "file:" + file_id if file_id else f"message:{event.message_id}:segment:{index}"
            references.append(GroupFileReference(
                self_id=event.self_id, group_id=event.group_id, message_id=event.message_id,
                file_id=file_id, filename=data.get("name") or data.get("file") or "unnamed-file",
                file_size=data.get("file_size", data.get("size")), busid=data.get("busid", 0),
                source_key=key, source_type=source_type, url=data.get("url")))
        except (ValidationError, ValueError):
            logger.warning("Invalid file metadata group_id=%s segment=%s", event.group_id, index)
    return references


def validate_download_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"https", "http"} or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
            or not any(host == domain or host.endswith("." + domain)
                       for domain in ("qq.com", "qpic.cn", "gtimg.cn"))):
        raise PermissionError("Untrusted attachment download URL")
    return url


async def validate_download_target(url: str) -> None:
    parsed = urlsplit(validate_download_url(url))
    addresses = await asyncio.get_running_loop().getaddrinfo(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
        raise PermissionError("Attachment URL resolves to a non-public address")


class FileResolver:
    def __init__(self, actions: "ActionGateway") -> None:
        self.actions = actions
        self._urls: dict[tuple[int, int, str], str] = {}

    def reconcile(self, references: list[GroupFileReference], states: dict[str, str]) -> None:
        for reference in references:
            status = states.get(reference.source_key)
            if status == "pending":
                self.remember([reference])
            elif status != "downloading":
                self.forget(reference.model_dump())

    def remember(self, references: list[GroupFileReference]) -> None:
        for reference in references:
            if reference.url and reference.file_id is None:
                if len(self._urls) >= 1000:
                    self._urls.pop(next(iter(self._urls)))
                self._urls[(reference.self_id, reference.group_id, reference.source_key)] = reference.url

    async def resolve(self, attachment: dict) -> str:
        if attachment["file_id"]:
            data = await self.actions.call("get_group_file_url", {
                "self_id": attachment["self_id"], "group_id": attachment["group_id"],
                "file_id": attachment["file_id"], "busid": attachment["busid"]})
            url = data.get("url") if isinstance(data, dict) else None
        else:
            url = self._urls.get((attachment["self_id"], attachment["group_id"], attachment["source_key"]))
        if not isinstance(url, str) or not url:
            raise ValueError("File URL unavailable; metadata retained")
        return validate_download_url(url)

    def forget(self, attachment: dict) -> None:
        self._urls.pop((attachment["self_id"], attachment["group_id"], attachment["source_key"]), None)
