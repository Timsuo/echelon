import asyncio
from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app.application import create_app
from app.policies.models import ConfigIntent, PolicyChanges
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_attachments import file_event
from tests.test_server import credentials


def test_attachment_download_does_not_block_collector_and_file_roundtrip(tmp_path, config, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_download(request):
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"abc")

    original_client = httpx.AsyncClient
    monkeypatch.setattr("app.attachments.worker.validate_download_target", AsyncMock())
    monkeypatch.setattr("app.application.httpx.AsyncClient", lambda **kwargs:
        original_client(transport=httpx.MockTransport(slow_download), **kwargs))
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        client.portal.call(set_policy, app.state.services.repository)
        with client.websocket_connect("/onebot/v11/ws", headers={
            "Authorization": "Bearer test-token", "X-Self-ID": "88",
        }) as ws:
            ws.send_json(file_event())
            action = ws.receive_json()
            assert action["action"] == "get_group_file_url"
            ws.send_json({"echo": action["echo"], "status": "ok", "retcode": 0,
                          "data": {"url": "https://gzc-download.ftn.qq.com/test"}})
            client.portal.call(asyncio.wait_for, started.wait(), 3)
            # Download deliberately held. Collector must still store a message and reply to status.
            ws.send_json(event(message_id=2))
            ws.send_json(event(message_type="private", message_id=3, message="/status"))
            status = ws.receive_json()
            assert status["action"] == "send_private_msg"
            assert "数据库消息：\n2" in status["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": status["echo"], "status": "ok", "retcode": 0})
            client.portal.call(release.set)

            async def await_download():
                for _ in range(300):
                    rows = await app.state.services.repository.query("SELECT download_status FROM attachments")
                    if rows and rows[0]["download_status"] == "downloaded":
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError("Attachment download never completed")

            client.portal.call(await_download)
            ws.send_json(event(message_type="private", message_id=4, message="/inbox"))
            inbox = ws.receive_json()
            assert "文件：test.txt" in inbox["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": inbox["echo"], "status": "ok", "retcode": 0})
            ws.send_json(event(message_type="private", message_id=5, message="/file 1 1"))
            uploaded = ws.receive_json()
            assert uploaded["action"] == "upload_private_file"
            assert uploaded["params"]["user_id"] == 99
            ws.send_json({"echo": uploaded["echo"], "status": "ok", "retcode": 0})


def test_configuration_worker_does_not_block_collection_and_requires_confirm(tmp_path, config, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def parse_config(self, text, on_retry):
        started.set()
        await release.wait()
        return ConfigIntent(action="update_group_policy", changes=PolicyChanges(mode="inbox"), reason="课程收件箱")

    monkeypatch.setattr("app.llm.deepseek.DeepSeekClient.parse_config", parse_config)
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        with client.websocket_connect("/onebot/v11/ws", headers={
            "Authorization": "Bearer test-token", "X-Self-ID": "88",
        }) as ws:
            ws.send_json(event(message_type="private", message_id=10, message="/config 123 作为课程收件箱"))
            queued = ws.receive_json()
            assert "确认前不会修改" in queued["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": queued["echo"], "status": "ok", "retcode": 0})
            client.portal.call(asyncio.wait_for, started.wait(), 3)
            ws.send_json(event(message_id=11))
            ws.send_json(event(message_type="private", message_id=12, message="/status"))
            status = ws.receive_json()
            assert "数据库消息：\n1" in status["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": status["echo"], "status": "ok", "retcode": 0})
            repository = app.state.services.repository
            assert client.portal.call(repository.query, "SELECT * FROM group_policies") == []
            client.portal.call(release.set)
            proposal = ws.receive_json()
            assert "/confirm 1" in proposal["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": proposal["echo"], "status": "ok", "retcode": 0})
            assert client.portal.call(repository.query, "SELECT * FROM group_policies") == []
            ws.send_json(event(message_type="private", message_id=13, message="/confirm 1"))
            confirmed = ws.receive_json()
            assert "配置已更新" in confirmed["params"]["message"][0]["data"]["text"]
            ws.send_json({"echo": confirmed["echo"], "status": "ok", "retcode": 0})
            assert client.portal.call(repository.query, "SELECT mode FROM group_policies")[0]["mode"] == "inbox"
