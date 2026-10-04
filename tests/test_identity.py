import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.application import create_app
from app.onebot.actions import ActionGateway
from app.onebot.server import onebot_socket
from tests.conftest import event
from tests.test_server import credentials


def test_wrong_x_self_id_rejected_before_attach(tmp_path, config):
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        services = app.state.services
        client.portal.call(services.repository.bind_onebot, 88)
        services.actions.attach = Mock(wraps=services.actions.attach)
        services.actions.call = AsyncMock(wraps=services.actions.call)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/onebot/v11/ws", headers={
                "Authorization": "Bearer test-token", "X-Self-ID": "777",
            }):
                pytest.fail("Wrong account was accepted")
        services.actions.attach.assert_not_called()
        services.actions.call.assert_not_called()
        assert not services.actions.connected


def test_pending_outbox_not_sent_to_wrong_account(tmp_path, config):
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        services = app.state.services
        client.portal.call(services.repository.bind_onebot, 88)
        client.portal.call(services.repository.notify, "pending private result", 88)
        services.actions.attach = Mock(wraps=services.actions.attach)
        services.actions.call = AsyncMock(wraps=services.actions.call)
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/onebot/v11/ws", headers={
                "Authorization": "Bearer test-token", "X-Self-ID": "777",
            }):
                pytest.fail("Pending outbox could reach wrong account")
        # Let the actual background notifier complete a disconnected polling cycle.
        client.portal.call(asyncio.sleep, 1.05)
        services.actions.attach.assert_not_called()
        services.actions.call.assert_not_called()
        assert not services.actions.connected
        rows = client.portal.call(services.repository.query, "SELECT * FROM private_outbox")
        assert len(rows) == 1 and rows[0]["sent_at"] is None and rows[0]["attempts"] == 0


def test_correct_header_binds_before_attach(tmp_path, config):
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        services = app.state.services
        services.actions.attach = Mock(wraps=services.actions.attach)
        with client.websocket_connect("/onebot/v11/ws", headers={
            "Authorization": "Bearer test-token", "X-Self-ID": "88", "X-Client-Role": "Universal",
        }) as ws:
            # This request/response is a barrier ensuring attach has completed.
            ws.send_json(event(message_type="private", message="/status"))
            result = ws.receive_json()
            assert services.actions.connected
            services.actions.attach.assert_called_once()
            assert client.portal.call(services.repository.state, "onebot_self_id") == "88"
            ws.send_json({"echo": result["echo"], "status": "ok", "retcode": 0})


@pytest.mark.parametrize("header", [None, "", "0", "-1", "1.2", "abc", " 88", "9223372036854775808"])
def test_missing_or_invalid_header_never_attaches(tmp_path, config, header):
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        services = app.state.services
        services.actions.attach = Mock(wraps=services.actions.attach)
        headers = {"Authorization": "Bearer test-token"}
        if header is not None:
            headers["X-Self-ID"] = header
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/onebot/v11/ws", headers=headers):
                pytest.fail("Invalid identity was accepted")
        services.actions.attach.assert_not_called()
        assert not services.actions.connected
        assert client.portal.call(services.repository.state, "onebot_self_id") is None


@pytest.mark.parametrize("self_id", [777, None, True, 88.5, "bad"])
async def test_bad_event_detaches_before_close_yields(repository, self_id):
    actions = ActionGateway(99)
    events = AsyncMock()
    socket = SimpleNamespace(
        headers={"authorization": "Bearer test-token", "x-self-id": "88"}, query_params={},
        app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(
            secrets=credentials(), repository=repository, actions=actions, events=events, history=AsyncMock()))),
        accept=AsyncMock(), receive_text=AsyncMock(return_value=json.dumps(event(self_id=self_id))),
        send_json=AsyncMock(),
    )

    async def close(code):
        assert code == 1008
        assert not actions.connected
        with pytest.raises(ConnectionError):
            await actions.call("send_private_msg", {"user_id": 99, "text": "must not send"})
        await asyncio.sleep(0)

    socket.close = AsyncMock(side_effect=close)
    await onebot_socket(socket)
    socket.accept.assert_awaited_once()
    socket.close.assert_awaited_once()
    socket.send_json.assert_not_called()
    events.handle.assert_not_called()
    assert await repository.query("SELECT * FROM messages") == []


async def test_first_account_binding_is_atomic(repository):
    await repository.query("DELETE FROM runtime_state WHERE key='onebot_self_id'")
    results = await asyncio.gather(repository.bind_onebot(111), repository.bind_onebot(222))
    assert sorted(results) == [False, True]
    assert await repository.state("onebot_self_id") in {"111", "222"}
