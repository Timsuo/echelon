import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.application import create_app
from app.config import Secrets
from app.llm.deepseek import DeepSeekClient
from app.llm.schemas import SummaryData, Topic
from tests.conftest import event


def credentials():
    return Secrets(_env_file=None, onebot_access_token="test-token", admin_qq=99,
                   deepseek_api_key="")


def test_websocket_auth_collection_and_echo(tmp_path, config):
    with TestClient(create_app(config, credentials(), tmp_path)) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/onebot/v11/ws"):
                pass
        with client.websocket_connect("/onebot/v11/ws", headers={"Authorization": "Bearer test-token", "X-Self-ID": "88"}) as ws:
            ws.send_text("{bad JSON")
            ws.send_json({"post_type": "notice", "self_id": 88})
            ws.send_json(event())
            ws.send_json(event())
            ws.send_json(event(group_id=999, message_id=2))
            ws.send_json(event(message_type="private", message_id=3, message="/status"))
            action = ws.receive_json()
            assert action["action"] == "send_private_msg"
            assert action["params"]["user_id"] == 99
            assert "1" in action["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": action["echo"], "status": "ok", "retcode": 0})
    with sqlite3.connect(tmp_path / "data/messages.db") as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_second_instance_rejected(tmp_path, config):
    with TestClient(create_app(config, credentials(), tmp_path)):
        with pytest.raises(RuntimeError, match="第二实例"):
            with TestClient(create_app(config, credentials(), tmp_path)):
                pass


def test_reconnect_and_account_binding(tmp_path, config):
    headers = {"Authorization": "Bearer test-token", "X-Self-ID": "88"}
    with TestClient(create_app(config, credentials(), tmp_path)) as client:
        with client.websocket_connect("/onebot/v11/ws", headers=headers) as ws:
            ws.send_json(event())
            ws.send_json(event(message_type="private", message="/status"))
            result = ws.receive_json()
            ws.send_json({"echo": result["echo"], "status": "ok", "retcode": 0})
        with client.websocket_connect("/onebot/v11/ws", headers=headers) as ws:
            ws.send_json(event(self_id=777, message_id=2))
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    with sqlite3.connect(tmp_path / "data/messages.db") as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


def test_summary_end_to_end(tmp_path, config, monkeypatch):
    async def summarize(self, text, on_retry):
        assert "untrusted_chat_records" in text
        return SummaryData(topics=[Topic(title="讨论", summary="测试总结", participants=["Card"], start_message_id="1", end_message_id="1")],
                           decisions=[], todos=[], important_events=[], uncertainties=[],
                           notable_message_ids=["1"])

    monkeypatch.setattr(DeepSeekClient, "summarize", summarize)
    with TestClient(create_app(config, credentials(), tmp_path)) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={"Authorization": "Bearer test-token", "X-Self-ID": "88"}) as ws:
            ws.send_json(event())
            ws.send_json(event(message_type="private", message_id=100, message="/summary 2h"))
            messages = []
            for _ in range(2):
                result = ws.receive_json()
                assert result["action"] == "send_private_msg"
                messages.append(result["params"]["message"][0]["data"]["text"])
                ws.send_json({"echo": result["echo"], "status": "ok", "retcode": 0})
            assert "排队" in messages[0]
            assert "测试总结" in messages[1]
            # A following status response is also a processing barrier for this new group event.
            ws.send_json(event(message_id=2))
            ws.send_json(event(message_type="private", message_id=101, message="/status"))
            status = ws.receive_json()
            assert "数据库消息：\n2" in status["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": status["echo"], "status": "ok", "retcode": 0})
    with sqlite3.connect(tmp_path / "data/messages.db") as db:
        assert db.execute("SELECT status FROM summary_jobs").fetchone()[0] == "completed"
        assert db.execute("SELECT source_message_count FROM summaries").fetchone()[0] == 1
