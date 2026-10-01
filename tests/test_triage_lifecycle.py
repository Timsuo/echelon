import asyncio
import json

from fastapi.testclient import TestClient

from app.application import create_app
from app.config import TriageConfig
from app.llm.deepseek import DeepSeekClient
from app.triage.models import TriageResult
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_server import credentials
from tests.test_triage import item


def test_lifespan_batches_messages_without_automatic_delivery(tmp_path, config, monkeypatch):
    config.triage = TriageConfig(inbox_debounce_seconds=1, inbox_max_wait_seconds=2)
    calls = []
    async def classify(self, text):
        data = json.loads(text)
        calls.append(data)
        return TriageResult(items=[item([m['id'] for m in data['messages']])], ignored_message_ids=[])
    monkeypatch.setattr(DeepSeekClient, 'triage', classify)
    app = create_app(config, credentials(), tmp_path)
    with TestClient(app) as client:
        repository = app.state.services.repository
        client.portal.call(set_policy, repository)
        with client.websocket_connect('/onebot/v11/ws', headers={'Authorization': 'Bearer test-token', 'X-Self-ID': '88'}) as ws:
            ws.send_json(event(message='实验报告周五提交'))
            ws.send_json(event(message_id=2, message='请使用 PDF 格式'))
            async def wait_completed():
                async with asyncio.timeout(8):
                    while not await repository.query("SELECT id FROM triage_jobs WHERE status='completed'"):
                        await asyncio.sleep(0.05)
            client.portal.call(wait_completed)
            assert len(calls) == 1 and len(calls[0]['messages']) == 2
            assert not client.portal.call(repository.query, 'SELECT * FROM private_outbox')
            ws.send_json(event(message_type='private', message_id=3, message='/detail 1'))
            output = ws.receive_json()
            text = output['params']['message'][0]['data']['text']
            assert 'HIGH' in text and 'ASSIGNMENT' in text and '2 条' in text
            ws.send_json({'echo': output['echo'], 'status': 'ok', 'retcode': 0})
