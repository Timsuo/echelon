import time

import pytest

from app.commands.router import CommandRouter
from app.config import AppConfig, Groups
from app.onebot.actions import ActionGateway
from app.onebot.events import EventProcessor
from app.storage.db import Database
from app.storage.repository import Repository


@pytest.fixture
async def repository(tmp_path):
    db = Database(tmp_path / "messages.db")
    await db.open()
    repository = Repository(db)
    await repository.bind_onebot(88)
    yield repository
    await db.close()


@pytest.fixture
def config():
    return AppConfig(groups=Groups(allowed=[123]))


@pytest.fixture
def processor(repository, config):
    router = CommandRouter(repository, config, 99, ActionGateway(99), time.time())
    return EventProcessor(repository, config.groups.allowed, router)


def event(**overrides):
    payload = {"post_type": "message", "message_type": "group", "self_id": 88,
               "group_id": 123, "message_id": 1, "user_id": 99, "time": int(time.time()) - 1,
               "message": [{"type": "text", "data": {"text": "hello"}}],
               "raw_message": "hello", "sender": {"nickname": "Nick", "card": "Card"}}
    return payload | overrides
