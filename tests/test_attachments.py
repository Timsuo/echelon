import hashlib
from unittest.mock import AsyncMock

import httpx
import pytest

from app.attachments.storage import AttachmentStorage, safe_filename
from app.attachments.worker import AttachmentWorker
from app.config import AttachmentConfig
from app.onebot.files import FileResolver, validate_download_target, validate_download_url
from app.storage.inbox_repository import InboxRepository
from tests.conftest import event
from tests.policy_helpers import set_policy


@pytest.fixture(autouse=True)
async def inbox_policy(repository):
    await set_policy(repository)


def file_event(**overrides):
    return event(message=[{"type": "file", "data": {"file_id": "f1", "file": "test.txt", "file_size": 3}}], **overrides)


async def test_file_message_record_and_links(processor, repository):
    await processor.handle(file_event())
    attachment = (await repository.query("SELECT * FROM attachments"))[0]
    message = (await repository.query("SELECT * FROM messages"))[0]
    item = (await repository.query("SELECT * FROM inbox_items"))[0]
    assert attachment["download_status"] == "pending"
    assert attachment["file_id"] == "f1"
    assert attachment["message_id"] == message["id"]
    assert item["title"] == "文件：test.txt"
    assert item["status"] == "unread" and item["priority"] is None and item["category"] is None
    assert (await repository.query("SELECT * FROM inbox_item_messages"))[0]["message_id"] == message["id"]
    assert (await repository.query("SELECT * FROM inbox_item_attachments"))[0]["attachment_id"] == attachment["id"]


async def test_notice_and_message_same_file_deduplicated(processor, repository):
    await processor.handle(file_event())
    notice = {"post_type": "notice", "notice_type": "group_upload", "self_id": 88,
              "group_id": 123, "user_id": 99, "time": 100,
              "file": {"id": "f1", "name": "test.txt", "size": 3, "busid": 102}}
    await processor.handle(notice)
    await processor.handle(notice)
    assert len(await repository.query("SELECT * FROM attachments")) == 1
    assert len(await repository.query("SELECT * FROM inbox_items")) == 1
    links = await repository.query("SELECT * FROM inbox_item_messages")
    assert len(links) == 2
    assert (await repository.query("SELECT busid FROM attachments"))[0]["busid"] == 102


async def test_nonwhitelist_files_and_notice_ignored(processor, repository):
    await processor.handle(file_event(group_id=999))
    await processor.handle({"post_type": "notice", "notice_type": "group_upload", "self_id": 88,
        "group_id": 999, "user_id": 99, "time": 100, "file": {"id": "f1", "name": "a.txt"}})
    for table in ("messages", "attachments", "inbox_items"):
        assert await repository.query(f"SELECT * FROM {table}") == []


async def test_invalid_metadata_does_not_stop_collection(processor, repository):
    await processor.handle(event(message=[{"type": "file", "data": {"name": "bad", "size": -1}}]))
    await processor.handle(event(message_id=2, message="ordinary message"))
    assert len(await repository.query("SELECT * FROM messages")) == 2
    assert await repository.query("SELECT * FROM attachments") == []


async def test_missing_file_id_uses_message_and_segment_identity(processor, repository):
    payload = event(message=[{"type": "file", "data": {"name": "same.txt"}}])
    await processor.handle(payload)
    await processor.handle(payload)
    await processor.handle(payload | {"message_id": 2})
    attachments = await repository.query("SELECT * FROM attachments")
    assert len(attachments) == 2
    assert attachments[0]["source_key"] != attachments[1]["source_key"]


async def test_size_and_disabled_policy(processor, repository):
    processor.attachment_config = AttachmentConfig(max_auto_download_mb=1)
    await processor.handle(event(message=[{"type": "file", "data": {"file_id": "large", "file": "large.zip", "size": 1048577}}]))
    row = (await repository.query("SELECT * FROM attachments"))[0]
    assert row["download_status"] == "skipped" and row["error"] == "size_limit"
    assert row["file_size"] == 1048577
    processor.attachment_config = AttachmentConfig(auto_download=False)
    await processor.handle(file_event(message_id=2))
    assert (await repository.query("SELECT * FROM attachments WHERE file_id='f1'"))[0]["download_status"] == "skipped"
    processor.attachment_config = AttachmentConfig(enabled=False)
    await processor.handle(file_event(message_id=3))
    assert len(await repository.query("SELECT * FROM messages")) == 3
    assert len(await repository.query("SELECT * FROM attachments")) == 2


def build_worker(repository, tmp_path, handler, retries=2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = AsyncMock()
    resolver.resolve.return_value = "https://gzc-download.ftn.qq.com/test"
    resolver.forget = lambda attachment: None
    repo = InboxRepository(repository.db)
    storage = AttachmentStorage(tmp_path / "attachments")
    async def check_target(url):
        validate_download_url(url)  # Mock HTTP tests must never query public DNS.

    worker = AttachmentWorker(repo, resolver, storage, AttachmentConfig(max_auto_download_mb=1, retry_count=retries), [123], client, check_target)
    return worker, client, storage


async def test_download_success_hash_and_idempotence(processor, repository, tmp_path):
    await processor.handle(file_event())
    worker, client, storage = build_worker(repository, tmp_path, lambda r: httpx.Response(200, content=b"abc"))
    try:
        await worker.process(await worker.repository.claim_attachment(88))
        row = (await repository.query("SELECT * FROM attachments"))[0]
        assert row["download_status"] == "downloaded"
        assert row["sha256"] == hashlib.sha256(b"abc").hexdigest()
        assert storage.verify(row).read_bytes() == b"abc"
        assert row["downloaded_at"] is not None
        await processor.handle(file_event())
        assert await worker.repository.claim_attachment(88) is None
        assert not list(storage.root.rglob("*.part"))
    finally:
        await client.aclose()


@pytest.mark.parametrize("status,attempts", [(404, 1), (403, 1), (503, 3)])
async def test_download_failure_and_finite_retry(processor, repository, tmp_path, status, attempts):
    await processor.handle(file_event())
    worker, client, storage = build_worker(repository, tmp_path, lambda r: httpx.Response(status))
    try:
        for _ in range(attempts):
            await repository.query("UPDATE attachments SET next_attempt=0")
            await worker.process(await worker.repository.claim_attachment(88))
        row = (await repository.query("SELECT * FROM attachments"))[0]
        assert row["download_status"] == "failed"
        assert row["attempts"] == attempts
        assert row["local_path"] is None
        assert await worker.repository.claim_attachment(88) is None
        await processor.handle(event(message_id=2))
        assert len(await repository.query("SELECT * FROM messages")) == 2
        assert not list(storage.root.rglob("*.part"))
    finally:
        await client.aclose()


class InterruptedBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"a" * 65536
        raise httpx.ReadError("interrupted")


async def test_partial_download_never_finalized(processor, repository, tmp_path):
    await processor.handle(file_event())
    worker, client, storage = build_worker(repository, tmp_path,
        lambda r: httpx.Response(200, stream=InterruptedBody()), retries=0)
    try:
        await worker.process(await worker.repository.claim_attachment(88))
        row = (await repository.query("SELECT * FROM attachments"))[0]
        assert row["download_status"] == "failed" and row["sha256"] is None
        assert not storage.path_for(row).exists()
        assert not list(storage.root.rglob("*.part"))
    finally:
        await client.aclose()


async def test_actual_stream_size_limit_even_if_metadata_lies(processor, repository, tmp_path):
    await processor.handle(file_event())
    worker, client, storage = build_worker(repository, tmp_path,
        lambda r: httpx.Response(200, stream=httpx.ByteStream(b"x" * (1048576 + 1))))
    try:
        await worker.process(await worker.repository.claim_attachment(88))
        row = (await repository.query("SELECT * FROM attachments"))[0]
        assert row["download_status"] == "skipped" and row["error"] == "size_limit"
        assert not storage.path_for(row).exists()
    finally:
        await client.aclose()


async def test_interrupted_state_resumes_on_restart(processor, repository, tmp_path):
    await processor.handle(file_event())
    repo = InboxRepository(repository.db)
    first = await repo.claim_attachment(88)
    storage = AttachmentStorage(tmp_path / "attachments")
    temporary, final = storage.prepare(first)
    temporary.write_bytes(b"partial")
    await repository.db.close()
    await repository.db.open()
    await repo.recover()
    worker, client, _ = build_worker(repository, tmp_path, lambda r: httpx.Response(200, content=b"abc"))
    try:
        await worker.process(await repo.claim_attachment(88))
        assert final.read_bytes() == b"abc"
        assert not temporary.exists()
    finally:
        await client.aclose()


@pytest.mark.parametrize("name", ["../../.env", r"C:\Windows\win.ini", "a/b\\c.txt", "CON", "NUL.txt", "LPT1", "..", "a:stream", " "])
def test_safe_filenames_stay_inside_root(tmp_path, name):
    storage = AttachmentStorage(tmp_path / "attachments")
    path = storage.path_for({"self_id": 88, "group_id": 123, "id": 1, "filename": name})
    assert path.is_relative_to(storage.root)
    assert path.parent == storage.root / "88" / "123"
    assert not any(char in safe_filename(name) for char in '/\\:')
    assert ".." not in safe_filename(name)


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "http://127.0.0.1/private", "http://169.254.169.254/", "https://qq.com.evil.test/", "https://user:pass@qq.com/", "https://qq.com:9999/"])
def test_download_url_restrictions(url):
    with pytest.raises(PermissionError):
        validate_download_url(url)


async def test_resolver_gets_fresh_url_and_only_uses_ephemeral_fallback():
    actions = AsyncMock()
    actions.call.return_value = {"url": "https://gzc-download.ftn.qq.com/fresh"}
    resolver = FileResolver(actions)
    attachment = {"self_id": 88, "group_id": 123, "file_id": "f1", "busid": 102, "source_key": "file:f1"}
    assert (await resolver.resolve(attachment)).endswith("/fresh")
    actions.call.assert_awaited_once_with("get_group_file_url", {"self_id": 88, "group_id": 123, "file_id": "f1", "busid": 102})
    with pytest.raises(ValueError):
        await resolver.resolve(attachment | {"file_id": None})


async def test_url_resolver_failure_does_not_stop_collector(processor, repository, tmp_path):
    await processor.handle(file_event())
    worker, client, _ = build_worker(repository, tmp_path, lambda r: httpx.Response(200, content=b"abc"))
    worker.resolver.resolve.side_effect = PermissionError("expired file")
    try:
        await worker.process(await worker.repository.claim_attachment(88))
        assert (await repository.query("SELECT download_status FROM attachments"))[0]["download_status"] == "failed"
        await processor.handle(event(message_id=2))
        assert len(await repository.query("SELECT * FROM messages")) == 2
    finally:
        await client.aclose()


async def test_redirect_to_local_address_is_rejected(processor, repository, tmp_path):
    await processor.handle(file_event())
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    worker, client, _ = build_worker(repository, tmp_path, handler)
    try:
        await worker.process(await worker.repository.claim_attachment(88))
        assert len(requests) == 1
        assert (await repository.query("SELECT download_status FROM attachments"))[0]["download_status"] == "failed"
    finally:
        await client.aclose()


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
async def test_allowed_domain_cannot_resolve_to_private_ip(monkeypatch, address):
    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
        (2, 1, 6, "", (address, 443))]))
    with pytest.raises(PermissionError, match="non-public"):
        await validate_download_target("https://localhost.qq.com/file")
