import asyncio

import pytest

from app.onebot.actions import ActionGateway


@pytest.mark.parametrize("action", ["send_group_msg", "set_group_ban", "set_group_kick", "delete_msg",
                                   "send_msg", "set_friend_add_request", "unknown"])
async def test_action_firewall(action):
    with pytest.raises(PermissionError):
        await ActionGateway(99).call(action, {})


class FakeSocket:
    def __init__(self):
        self.sent = asyncio.Queue()

    async def send_json(self, payload):
        await self.sent.put(payload)


async def test_private_ack_and_cq_literal():
    gateway = ActionGateway(99, timeout=1)
    socket = FakeSocket()
    gateway.attach(socket)
    task = asyncio.create_task(gateway.call("send_private_msg", {"user_id": 99, "text": "[CQ:at,qq=all]"}))
    payload = await socket.sent.get()
    assert payload["params"]["message"] == [{"type": "text", "data": {"text": "[CQ:at,qq=all]"}}]
    assert "group_id" not in payload["params"]
    gateway.receive_response({"echo": payload["echo"], "status": "ok", "retcode": 0})
    await task
    with pytest.raises(PermissionError):
        await gateway.call("send_private_msg", {"user_id": 1, "text": "no"})


async def test_disconnect_unblocks_pending_action():
    gateway = ActionGateway(99)
    socket = FakeSocket()
    gateway.attach(socket)
    task = asyncio.create_task(gateway.call("send_private_msg", {"user_id": 99, "text": "hello"}))
    await socket.sent.get()
    gateway.detach(socket)
    with pytest.raises(ConnectionError):
        await task


async def test_action_timeout():
    gateway = ActionGateway(99, timeout=0.01)
    gateway.attach(FakeSocket())
    with pytest.raises(TimeoutError):
        await gateway.call("send_private_msg", {"user_id": 99, "text": "hello"})
    assert gateway._pending == {}
