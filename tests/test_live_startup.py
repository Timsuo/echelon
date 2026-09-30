"""Real uvicorn/TCP smoke check with temporary files, no NapCat or external API required."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from websockets.asyncio.client import connect


async def test_real_server_startup(tmp_path):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    script = """
import sys
from pathlib import Path
import uvicorn
from app.application import create_app
from app.config import AppConfig, Secrets
config = AppConfig()
secrets = Secrets(_env_file=None, onebot_access_token='local-smoke-token', admin_qq=99, deepseek_api_key='')
uvicorn.run(create_app(config, secrets, Path(sys.argv[1])), host='127.0.0.1', port=int(sys.argv[2]),
            log_config=None, access_log=False, ws='websockets-sansio', ws_max_size=2097152)
"""
    log_path = tmp_path / "process.log"
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(tmp_path), str(port)],
            cwd=Path(__file__).resolve().parents[1], stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(log_path.read_text(encoding="utf-8"))
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail("Server did not start in 15 seconds")
            async with connect(f"ws://127.0.0.1:{port}/onebot/v11/ws",
                               additional_headers={"Authorization": "Bearer local-smoke-token",
                                                   "X-Self-ID": "88"}) as ws:
                await ws.send(json.dumps({"post_type": "message", "message_type": "private",
                    "self_id": 88, "user_id": 99, "message_id": 123, "time": int(time.time()),
                    "message": "/status"}))
                import asyncio
                result = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert result["action"] == "send_private_msg"
                assert "Connected" in result["params"]["message"][0]["data"]["text"]
                await ws.send(json.dumps({"echo": result["echo"], "status": "ok", "retcode": 0}))
        finally:
            process.terminate()
            process.wait(timeout=10)
