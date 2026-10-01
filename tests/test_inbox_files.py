import asyncio
import hashlib
import os
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from pydantic import ValidationError

from app.attachments.storage import AttachmentStorage
from app.commands.router import CommandRouter
from app.config import AppConfig, AttachmentConfig, InboxConfig
from app.notifier.qq import deliver_notification
from app.onebot.actions import ActionGateway
from app.onebot.adapter import MessageEvent
from app.storage.inbox_repository import InboxRepository
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_actions import FakeSocket
from tests.test_attachments import file_event


@pytest.fixture(autouse=True)
async def inbox_policy(repository):
    await set_policy(repository)


async def downloaded(processor, repository, tmp_path):
    await processor.handle(file_event())
    row = (await repository.query("SELECT * FROM attachments"))[0]
    storage = AttachmentStorage(tmp_path / "attachments")
    temporary, final = storage.prepare(row)
    temporary.write_bytes(b"abc")
    storage.finish(temporary, final)
    await InboxRepository(repository.db).download_result(row, "downloaded", path=str(final),
        sha256=hashlib.sha256(b"abc").hexdigest(), size=3)
    return storage, await InboxRepository(repository.db).attachment(88, row["id"])


def gateway(repository, storage):
    actions = ActionGateway(99, timeout=1, repository=repository, storage=storage, allowed_groups=[123])
    socket = FakeSocket()
    socket.headers = {"x-self-id": "88"}
    actions.attach(socket)
    return actions, socket


def command(text, user_id=99, self_id=88):
    return MessageEvent.model_validate(event(message_type="private", message=text, user_id=user_id, self_id=self_id))


async def test_private_file_allowed_only_for_admin(processor, repository, tmp_path):
    storage, attachment = await downloaded(processor, repository, tmp_path)
    actions, socket = gateway(repository, storage)
    task = asyncio.create_task(actions.call("upload_private_file", {"user_id": 99, "self_id": 88, "attachment_id": attachment["id"]}))
    sent = await asyncio.wait_for(socket.sent.get(), 2)
    assert sent["action"] == "upload_private_file"
    assert sent["params"] == {"user_id": 99, "file": attachment["local_path"], "name": "test.txt"}
    actions.receive_response({"echo": sent["echo"], "status": "ok", "retcode": 0})
    await task
    with pytest.raises(PermissionError):
        await actions.call("upload_private_file", {"user_id": 777, "self_id": 88, "attachment_id": attachment["id"]})
    assert socket.sent.empty()


@pytest.mark.parametrize("action", ["send_group_msg", "send_msg", "upload_group_file", "trans_group_file", "delete_group_file", "move_group_file", "rename_group_file", "set_group_ban", "set_group_kick", "delete_msg", "download_file", "unknown_mutation"])
async def test_phase2_firewall_rejects_mutations(action):
    with pytest.raises(PermissionError):
        await ActionGateway(99).call(action, {})


async def test_read_action_is_whitelisted_and_account_scoped(repository, tmp_path):
    actions, socket = gateway(repository, AttachmentStorage(tmp_path / "attachments"))
    request = {"self_id": 88, "group_id": 123, "file_id": "f1", "busid": 102}
    for invalid in (request | {"self_id": 222}, request | {"group_id": 999}):
        with pytest.raises(PermissionError):
            await actions.call("get_group_file_url", invalid)
    task = asyncio.create_task(actions.call("get_group_file_url", request))
    sent = await socket.sent.get()
    assert "self_id" not in sent["params"]
    actions.receive_response({"echo": sent["echo"], "status": "ok", "retcode": 0, "data": {"url": "https://qq.com/file"}})
    assert await task == {"url": "https://qq.com/file"}


@pytest.mark.parametrize("outside", [".env", "config/config.yaml", "data/messages.db", "app/main.py", "C:/Windows/win.ini"])
async def test_file_cannot_exfiltrate_arbitrary_local_paths(processor, repository, config, tmp_path, outside):
    storage, attachment = await downloaded(processor, repository, tmp_path)
    # Fixtures only; never open the user's actual secrets/config/database/system files.
    external = tmp_path / "outside" / outside.replace(":", "_")
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("must not leave this machine", encoding="utf-8")
    await repository.query("UPDATE attachments SET local_path=?", (str(external),))
    actions, socket = gateway(repository, storage)
    router = CommandRouter(repository, config, 99, actions, 0, storage)
    await router.dispatch(command("/file 1 1"))
    assert await repository.query("SELECT * FROM private_outbox WHERE kind='file'") == []
    with pytest.raises(PermissionError):
        await actions.call("upload_private_file", {"user_id": 99, "self_id": 88, "attachment_id": attachment["id"]})
    assert socket.sent.empty()


async def test_hardlink_and_changed_file_rejected(processor, repository, tmp_path):
    storage, attachment = await downloaded(processor, repository, tmp_path)
    path = storage.validate(attachment)
    path.write_bytes(b"tampered")
    with pytest.raises(PermissionError, match="integrity"):
        storage.verify(attachment)
    external = tmp_path / "secret.txt"
    external.write_bytes(b"abc")
    path.unlink()
    os.link(external, path)
    with pytest.raises(PermissionError, match="non-linked"):
        storage.verify(attachment)


@pytest.mark.parametrize("text", [r"/file C:\Windows\win.ini", "/file ../../.env 1", "/file 1 ../2", "/file 0 1", "/file 1 -1"])
async def test_file_command_accepts_only_numeric_ids(processor, repository, text):
    await processor.router.dispatch(command(text))
    assert await repository.query("SELECT * FROM private_outbox WHERE kind='file'") == []


async def test_inbox_detail_archive_and_file_commands(processor, repository, config, tmp_path):
    storage, attachment = await downloaded(processor, repository, tmp_path)
    actions, socket = gateway(repository, storage)
    router = CommandRouter(repository, config, 99, actions, 0, storage)
    await router.dispatch(command("/inbox"))
    assert "文件：test.txt" in (await repository.query("SELECT text FROM private_outbox"))[0]["text"]
    await router.dispatch(command("/detail 1"))
    row = (await repository.query("SELECT * FROM inbox_items"))[0]
    assert row["status"] == "read"
    await router.dispatch(command("/inbox unread"))
    assert "暂无条目" in (await repository.query("SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1"))[0]["text"]
    await router.dispatch(command("/file 1 1"))
    file_job = (await repository.query("SELECT * FROM private_outbox WHERE kind='file'"))[0]
    delivery = asyncio.create_task(deliver_notification(repository, actions, 99, file_job))
    sent = await asyncio.wait_for(socket.sent.get(), 2)
    assert sent["action"] == "upload_private_file"
    actions.receive_response({"echo": sent["echo"], "status": "ok", "retcode": 0})
    await delivery
    assert (await repository.query("SELECT sent_at FROM private_outbox WHERE kind='file'"))[0]["sent_at"] is not None
    await router.dispatch(command("/archive 1"))
    await router.dispatch(command("/inbox"))
    assert "暂无条目" in (await repository.query("SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1"))[0]["text"]
    for table in ("messages", "attachments", "inbox_items"):
        assert len(await repository.query(f"SELECT * FROM {table}")) == 1
    await router.dispatch(command("/detail 1"))
    assert (await repository.query("SELECT status FROM inbox_items"))[0]["status"] == "archived"


@pytest.mark.parametrize("text", ["/inbox", "/detail 1", "/archive 1", "/file 1 1"])
async def test_nonadmin_cannot_use_inbox(processor, repository, text):
    await processor.handle(file_event())
    await processor.router.dispatch(command(text, user_id=777))
    assert await repository.query("SELECT * FROM private_outbox") == []
    assert (await repository.query("SELECT status FROM inbox_items"))[0]["status"] == "unread"


async def test_account_a_cannot_view_or_forward_b(processor, repository, config, tmp_path):
    await set_policy(repository, self_id=222)
    await processor.handle(file_event(self_id=222))
    actions, socket = gateway(repository, AttachmentStorage(tmp_path / "attachments"))
    router = CommandRouter(repository, config, 99, actions, 0, actions.storage)
    for text in ("/inbox", "/detail 1", "/archive 1", "/file 1 1"):
        await router.dispatch(command(text))
    texts = await repository.query("SELECT text FROM private_outbox")
    assert all("test.txt" not in row["text"] for row in texts)
    assert (await repository.query("SELECT status FROM inbox_items"))[0]["status"] == "unread"
    for self_id in (88, 222):
        with pytest.raises(PermissionError):
            await actions.call("upload_private_file", {"user_id": 99, "self_id": self_id, "attachment_id": 1})
    assert socket.sent.empty()
    assert await InboxRepository(repository.db).claim_attachment(88) is None


async def test_many_to_many_and_cross_account_link_constraints(processor, repository):
    await processor.handle(file_event())
    await processor.handle(event(message_id=2))
    await processor.handle(event(self_id=222, message_id=3))
    inbox = InboxRepository(repository.db)
    item_id = await inbox.create_item(88, "aggregate", "two messages", [1, 2], [1])
    detail = await inbox.detail(88, item_id)
    assert detail["message_count"] == 2 and len(detail["attachments"]) == 1
    with pytest.raises(aiosqlite.IntegrityError):
        await inbox.create_item(88, "bad", "", [3], [])
    with pytest.raises(aiosqlite.IntegrityError):
        await inbox.create_item(222, "bad", "", [], [1])


@pytest.mark.parametrize("config", [{"max_auto_download_mb": 0}, {"retry_count": -1}, {"max_auto_download_mb": 2000}, {"storage_dir": ""}])
def test_attachment_config_rejects_invalid_values(config):
    with pytest.raises(ValidationError):
        AttachmentConfig(**config)


def test_inbox_config_bounds_and_defaults():
    assert AppConfig().attachments.max_auto_download_mb == 100
    assert AppConfig().inbox.default_page_size == 10
    with pytest.raises(ValidationError):
        InboxConfig(default_page_size=0)


async def test_unsafe_file_outbox_fails_without_blocking_following_messages(processor, repository, tmp_path):
    await downloaded(processor, repository, tmp_path)
    await repository.enqueue_file(88, 1)
    item = await repository.next_notification()
    actions = AsyncMock()
    actions.call.side_effect = PermissionError("blocked")
    await deliver_notification(repository, actions, 99, item)
    rows = await repository.query("SELECT * FROM private_outbox ORDER BY id")
    assert rows[0]["cancelled_at"] is not None
    assert rows[1]["kind"] == "text" and "附件发送失败" in rows[1]["text"]


async def test_file_outbox_waits_for_its_account_after_restart(processor, repository, tmp_path):
    await downloaded(processor, repository, tmp_path)
    await repository.enqueue_file(88, 1)
    await repository.state("onebot_self_id", "222")
    await repository.db.close()
    await repository.db.open()
    assert await repository.next_notification() is None
    await repository.state("onebot_self_id", "88")
    assert (await repository.next_notification())["attachment_id"] == 1


async def test_file_action_cannot_accept_user_path(processor, repository, tmp_path):
    storage, _ = await downloaded(processor, repository, tmp_path)
    actions, socket = gateway(repository, storage)
    with pytest.raises(ValidationError):
        await actions.call("upload_private_file", {"user_id": 99, "self_id": 88, "attachment_id": 1, "file": ".env"})
    assert socket.sent.empty()


async def test_linked_storage_directory_rejected(processor, repository, tmp_path, monkeypatch):
    storage, attachment = await downloaded(processor, repository, tmp_path)
    path_type = type(storage.root)
    original = path_type.is_symlink
    bad_directory = storage.root / "88"
    monkeypatch.setattr(path_type, "is_symlink", lambda path: path == bad_directory or original(path))
    with pytest.raises(PermissionError, match="link"):
        storage.verify(attachment)


async def test_file_not_ready_and_size_limit_feedback(processor, repository):
    await processor.handle(file_event())
    await processor.router.dispatch(command("/file 1 1"))
    assert "附件尚未准备完成" in (await repository.query("SELECT text FROM private_outbox"))[0]["text"]
    await repository.query("UPDATE attachments SET download_status='skipped',error='size_limit'")
    await processor.router.dispatch(command("/file 1 1"))
    assert "超过自动下载大小限制" in (await repository.query("SELECT text FROM private_outbox ORDER BY id DESC LIMIT 1"))[0]["text"]


async def test_file_delivery_retry_is_bounded(processor, repository, tmp_path):
    await downloaded(processor, repository, tmp_path)
    await repository.enqueue_file(88, 1)
    actions = AsyncMock()
    actions.call.side_effect = TimeoutError()
    for _ in range(3):
        await repository.query("UPDATE private_outbox SET next_attempt=0")
        await deliver_notification(repository, actions, 99, await repository.next_notification())
    row = (await repository.query("SELECT * FROM private_outbox WHERE kind='file'"))[0]
    assert row["cancelled_at"] is not None and actions.call.await_count == 3
