import asyncio
import logging
import uuid
from typing import Any

from fastapi import WebSocket
from pydantic import BaseModel, ConfigDict, Field

from app.onebot.adapter import response_echo

logger = logging.getLogger(__name__)
ALLOWED_ACTIONS = frozenset({"send_private_msg"})


class PrivateMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000)


class ActionGateway:
    """Sole outbound WebSocket writer. No generic raw-send interface is exposed."""

    def __init__(self, admin_qq: int, timeout: float = 15) -> None:
        self.admin_qq = admin_qq
        self.timeout = timeout
        self._socket: WebSocket | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def attach(self, socket: WebSocket) -> None:
        if self._socket is not None:
            raise RuntimeError("Only one OneBot connection is supported")
        self._socket = socket

    def detach(self, socket: WebSocket) -> None:
        if self._socket is not socket:
            return
        self._socket = None
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

    async def call(self, action: str, params: dict[str, Any]) -> None:
        # Check before connection state or parameter validation, always fail closed.
        if action not in ALLOWED_ACTIONS:
            logger.error("Action Firewall blocked non-whitelisted action")
            raise PermissionError("Action is not permitted")
        request = PrivateMessage.model_validate(params)
        if request.user_id != self.admin_qq:
            logger.error("Action Firewall blocked non-admin destination")
            raise PermissionError("Only ADMIN_QQ may receive messages")
        if self._socket is None:
            raise ConnectionError("OneBot disconnected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            async with asyncio.timeout(self.timeout):
                async with self._send_lock:
                    if self._socket is None:
                        raise ConnectionError("OneBot disconnected")
                    await self._socket.send_json({
                        "action": "send_private_msg",
                        "params": {"user_id": request.user_id,
                                   "message": [{"type": "text", "data": {"text": request.text}}],
                                   "auto_escape": True},
                        "echo": echo,
                    })
                response = await future
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise RuntimeError("OneBot rejected send_private_msg")
            logger.info("send_private_msg succeeded")
        except Exception as error:
            logger.warning("send_private_msg failed: %s", type(error).__name__)
            raise
        finally:
            self._pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Retrieve a disconnect failure even if send itself failed.
