import json
import logging
import uuid

import aiosqlite
from anyio import CancelScope
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.onebot.adapter import authenticated, parse_self_id

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/onebot/v11/ws")
async def onebot_socket(socket: WebSocket) -> None:
    services = socket.app.state.services
    actions = services.actions
    if not authenticated(socket.headers, socket.query_params,
                         services.secrets.onebot_access_token.get_secret_value()):
        logger.warning("Security: WebSocket authentication denied")
        await socket.close(code=1008)
        return
    # Phase 1 expects a universal connection, not separate API/Event channels.
    role = socket.headers.get("x-client-role", "Universal").lower()
    if role != "universal" or actions.connected:
        logger.warning("WebSocket rejected: role or duplicate connection")
        await socket.close(code=1008)
        return
    header_id = socket.headers.get("x-self-id")
    if header_id is None:
        logger.warning("OneBot connection rejected: missing X-Self-ID")
        await socket.close(code=1008)
        return
    try:
        incoming_id = parse_self_id(header_id)
    except ValueError:
        logger.warning("OneBot connection rejected: invalid X-Self-ID")
        await socket.close(code=1008)
        return
    session = uuid.uuid4().hex
    attached = False
    try:
        if not await services.repository.bind_onebot(incoming_id):
            logger.warning("OneBot connection rejected: unexpected X-Self-ID")
            await socket.close(code=1008)
            return
        await socket.accept()
        # Another validated handshake may have completed while accept yielded.
        if actions.connected:
            logger.warning("OneBot connection rejected: duplicate connection")
            await socket.close(code=1008)
            return
        actions.attach(socket)
        attached = True
        await services.history.connected(incoming_id, session)
        logger.info("WebSocket connected")
        while True:
            try:
                payload = json.loads(await socket.receive_text())
                if not isinstance(payload, dict):
                    raise ValueError("Expected object")
                if "self_id" in payload:
                    try:
                        valid_identity = parse_self_id(payload["self_id"]) == incoming_id
                    except ValueError:
                        valid_identity = False
                    if not valid_identity:
                        logger.error("Security: unexpected QQ account")
                        # Revoke synchronously before close yields to the notifier.
                        actions.detach(socket)
                        await socket.close(code=1008)
                        break
                if actions.receive_response(payload):
                    continue
                await services.events.handle(payload)
            except WebSocketDisconnect:
                break
            except aiosqlite.Error:
                # No protocol ACK/replay guarantee; reconnect instead of silently discarding writes.
                logger.exception("Database failure while receiving event; reconnect required")
                await socket.close(code=1011)
                break
            except Exception as error:
                # Never log raw validation errors: they may contain arbitrary incoming secrets.
                logger.warning("OneBot event rejected: %s", type(error).__name__)
    except WebSocketDisconnect:
        logger.info("WebSocket peer disconnected")
    except Exception as error:
        logger.error("WebSocket failure: %s", type(error).__name__)
    finally:
        actions.detach(socket)
        if attached:
            with CancelScope(shield=True):
                await services.history.disconnected(incoming_id, session)
        logger.info("WebSocket disconnected")
