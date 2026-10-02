import asyncio
import logging
import uuid
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, ConfigDict, Field

from app.attachments.storage import AttachmentStorage, safe_filename
from app.onebot.adapter import parse_self_id, response_echo
from app.onebot.groups import GroupInfoQuery, GroupListQuery
from app.onebot.history import GroupHistoryQuery
from app.storage.authorization_repository import is_active
from app.storage.inbox_repository import InboxRepository
from app.storage.repository import Repository

logger = logging.getLogger(__name__)
READ_ONLY_ACTIONS = frozenset({"get_group_file_url", "get_group_msg_history", "get_group_info", "get_group_list"})
PRIVATE_OUTPUT_ACTIONS = frozenset({"send_private_msg", "upload_private_file"})
ALLOWED_ACTIONS = READ_ONLY_ACTIONS | PRIVATE_OUTPUT_ACTIONS


class ActionRejectedError(RuntimeError):
    """Provider rejected an allowed action; contains no raw provider response."""


class GroupFileQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    self_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    file_id: str = Field(min_length=1, max_length=1024)
    busid: int = Field(default=0, ge=0)


class PrivateFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: int = Field(gt=0)
    self_id: int = Field(gt=0)
    attachment_id: int = Field(gt=0)


class PrivateMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000)


class ActionGateway:
    """Sole outbound WebSocket writer. No generic raw-send interface is exposed."""

    def __init__(self, admin_qq: int, timeout: float = 15, repository: Repository | None = None,
                 storage: AttachmentStorage | None = None, allowed_groups: list[int] | None = None) -> None:
        self.admin_qq = admin_qq
        self.timeout = timeout
        self._socket: WebSocket | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.repository = repository
        self.storage = storage
        self.self_id: int | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def attach(self, socket: WebSocket) -> None:
        if self._socket is not None:
            raise RuntimeError("Only one OneBot connection is supported")
        header = getattr(socket, "headers", {}).get("x-self-id")
        self.self_id = parse_self_id(header) if header is not None else None
        self._socket = socket

    def detach(self, socket: WebSocket) -> None:
        if self._socket is not socket:
            return
        self._socket = None
        self.self_id = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("OneBot disconnected"))

    def receive_response(self, payload: dict[str, Any]) -> bool:
        echo = response_echo(payload)
        if echo is None:
            return False
        future = self._pending.get(echo)
        if future is not None and not future.done():
            future.set_result(payload)
        return True

    async def _check_account(self, self_id: int) -> None:
        if (self.repository is None or self.self_id != self_id
                or await self.repository.state("onebot_self_id") != str(self_id)):
            raise PermissionError("Action account does not match connected bot")

    async def call(self, action: str, params: dict[str, Any], *, expected_self_id: int | None = None) -> Any:
        # Check before connection state or parameter validation, always fail closed.
        if action not in ALLOWED_ACTIONS:
            logger.error("Action Firewall blocked non-whitelisted action")
            raise PermissionError("Action is not permitted")
        if action in PRIVATE_OUTPUT_ACTIONS and params.get("user_id") != self.admin_qq:
            logger.error("Action Firewall blocked non-admin destination")
            raise PermissionError("Only ADMIN_QQ may receive messages")
        target_socket = self._socket
        if expected_self_id is not None:
            await self._check_account(expected_self_id)
        if action == "send_private_msg":
            request = PrivateMessage.model_validate(params)
            wire_params = {"user_id": request.user_id,
                           "message": [{"type": "text", "data": {"text": request.text}}], "auto_escape": True}
        elif action in {"get_group_info", "get_group_list"}:
            query = (GroupInfoQuery if action == "get_group_info" else GroupListQuery).model_validate(params)
            await self._check_account(query.self_id)
            wire_params = query.model_dump(exclude={"self_id"})
            if action == "get_group_list":
                wire_params["no_cache"] = True
        elif action == "get_group_msg_history":
            history = GroupHistoryQuery.model_validate(params)
            await self._check_account(history.self_id)
            if not await self.repository.authorizations.is_active(history.self_id, history.group_id):
                raise PermissionError("Group is not allowed")
            wire_params = history.wire()
        elif action == "get_group_file_url":
            query = GroupFileQuery.model_validate(params)
            await self._check_account(query.self_id)
            if not await self.repository.authorizations.is_active(query.self_id, query.group_id):
                raise PermissionError("Group is not allowed")
            wire_params = query.model_dump(exclude={"self_id"})
        else:
            file = PrivateFile.model_validate(params)
            await self._check_account(file.self_id)
            if self.repository is None or self.storage is None:
                raise PermissionError("File output unavailable")
            attachment = await InboxRepository(self.repository.db).attachment(file.self_id, file.attachment_id)
            if attachment is None:
                raise PermissionError("Attachment not permitted")
            path = await asyncio.to_thread(self.storage.verify, attachment)
            wire_params = {"user_id": self.admin_qq, "file": str(path), "name": safe_filename(attachment["filename"])}
        if self._socket is None:
            raise ConnectionError("OneBot disconnected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with asyncio.timeout(self.timeout):
                async with self._send_lock:
                    if self._socket is None or self._socket is not target_socket:
                        raise ConnectionError("OneBot disconnected")
                    payload = {"action": action, "params": wire_params, "echo": echo}
                    if action in READ_ONLY_ACTIONS:
                        # Serialize the final authorization check and wire send with revoke.
                        async with self.repository.db.transaction() as connection:
                            async with connection.execute("SELECT value FROM runtime_state WHERE key='onebot_self_id'") as cursor:
                                bound = await cursor.fetchone()
                            if self._socket is not target_socket or self.self_id != params['self_id'] or not bound or bound[0] != str(params['self_id']):
                                raise PermissionError("Action account changed")
                            if action in {'get_group_file_url', 'get_group_msg_history'} and not await is_active(connection, params['self_id'], params['group_id']):
                                raise PermissionError("Group authorization removed")
                            await self._send_payload(payload)
                    else:
                        await self._send_payload(payload)
                response = await future
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise ActionRejectedError("OneBot rejected allowed action")
            logger.info("%s succeeded", action)
            return response.get("data")
        except Exception as error:
            logger.warning("%s failed: %s", action, type(error).__name__)
            raise
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve a disconnect failure even if send itself failed.

    async def _send_payload(self, payload):
        await self._socket.send_json(payload)
