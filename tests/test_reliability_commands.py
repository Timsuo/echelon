from unittest.mock import AsyncMock

import pytest

from app.attachments.models import GroupFileReference
from app.commands.help import COMMANDS, render_help
from app.onebot.files import FileResolver
from app.policies.models import GroupPolicy
from app.policies.parser import ConfigIntentParser
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_inbox_files import command


@pytest.mark.parametrize("text", ["高数群接下来14天重点关注", "高数群10月15日前重点关注", "高数群超过100MB文件不要下载",
                                  "756155087 mode inbox", "756155087 14 天内关注"])
def test_target_only_explicit_first_token_or_unique_alias(text):
    assert ConfigIntentParser.target(text, [GroupPolicy(self_id=88, group_id=756155087, alias="高数群")]) == 756155087


def test_explicit_unknown_target_and_duplicate_alias():
    policies = [GroupPolicy(self_id=88, group_id=i, alias="高数群") for i in (756155087, 887766554)]
    for text in ("123456789 mode inbox", "高数群14天重点关注", "接下来14天关注", "10月15日前重点关注"):
        with pytest.raises(ValueError):
            ConfigIntentParser.target(text, policies)


def transient_event():
    return event(message=[{"type": "file", "data": {"name": "a.txt", "size": 3, "url": "https://qq.com/transient"}}])


@pytest.mark.parametrize("reason", ["group_policy", "size_limit", "auto_download_disabled"])
async def test_skipped_url_not_cached(reason, processor, repository):
    if reason != "group_policy":
        await set_policy(repository)
    if reason == "auto_download_disabled":
        processor.attachment_config.auto_download = False
    payload = transient_event()
    if reason == "size_limit":
        payload["message"][0]["data"]["size"] = 101 * 1024**2
    processor.resolver = FileResolver(AsyncMock())
    await processor.handle(payload)
    assert processor.resolver._urls == {}
    row = (await repository.query("SELECT * FROM attachments"))[0]
    assert row["error"] == reason
    message = (await repository.query("SELECT * FROM messages"))[0]
    assert "https://qq.com/transient" not in message["message_json"]


@pytest.mark.parametrize("status", ["failed", "downloaded", "skipped"])
async def test_terminal_file_cache_cleared_and_not_reintroduced(status, processor, repository):
    await set_policy(repository)
    processor.resolver = FileResolver(AsyncMock())
    await processor.handle(transient_event())
    assert len(processor.resolver._urls) == 1
    await repository.query("UPDATE attachments SET download_status=?", (status,))
    await processor.handle(transient_event())
    assert processor.resolver._urls == {}


def test_transient_cache_bound():
    resolver = FileResolver(AsyncMock())
    references = [GroupFileReference(self_id=88, group_id=123, message_id=str(i), filename="a.txt", source_type="message",
                                    source_key=f"message:{i}", url="https://qq.com/transient") for i in range(1100)]
    resolver.reconcile(references, {ref.source_key: "pending" for ref in references})
    assert len(resolver._urls) == 1000
    resolver.reconcile(references, {ref.source_key: "failed" for ref in references})
    assert resolver._urls == {}


async def test_file_url_removed_from_cq_persistence_but_pending_resolver_can_use_it(processor, repository):
    await set_policy(repository)
    processor.resolver = FileResolver(AsyncMock())
    cq = "[CQ:file,name=a.txt,url=https://qq.com/transient]"
    await processor.handle(event(message=cq, raw_message=cq))
    row = (await repository.query("SELECT * FROM messages"))[0]
    assert "https://qq.com/transient" not in row["message_json"] + row["raw_message"]
    assert list(processor.resolver._urls.values()) == ["https://qq.com/transient"]


@pytest.mark.parametrize("name,expected", [("", "状态与可靠性"), ("summary", "Coverage Warning"), ("config", "10分钟"),
    ("coverage", "LIKELY_COVERED"), ("sync", "best-effort"), ("groups", "Priority Watch"), ("inbox", "read"), ("file", "不能输入文件路径")])
async def test_help_commands(name, expected, processor, repository):
    await processor.router.dispatch(command("/help " + name))
    text = (await repository.query("SELECT text FROM private_outbox"))[0]["text"]
    assert expected in text


def test_all_registered_commands_have_usable_help(processor):
    assert set(processor.router.handlers) == {"/" + spec.name for spec in COMMANDS}
    for spec in COMMANDS:
        assert spec.usage and spec.description and spec.examples
        assert spec.usage in render_help(spec.name)
        assert len(render_help(spec.name)) <= 1800


async def test_unknown_command_is_short_and_plain_private_ignored(processor, repository):
    await processor.router.dispatch(command("你好"))
    assert await repository.query("SELECT * FROM private_outbox") == []
    await processor.router.dispatch(command("/nonsense"))
    text = (await repository.query("SELECT text FROM private_outbox"))[0]["text"]
    assert "/help" in text and len(text) < 100 and "/summary" not in text


@pytest.mark.parametrize("text", ["/help", "/help summary", "/coverage", "/sync", "/sync 123"])
async def test_nonadmin_reliability_commands_ignored(text, processor, repository):
    await processor.router.dispatch(command(text, user_id=100))
    assert await repository.query("SELECT * FROM private_outbox") == []
    assert await repository.query("SELECT * FROM history_sync_jobs") == []


@pytest.mark.parametrize("text", ["/sync", "/sync 123"])
async def test_sync_queues_persistently_without_calling_api(text, processor, repository):
    processor.router.actions.call = AsyncMock()
    await processor.router.dispatch(command(text))
    jobs = await repository.query("SELECT * FROM history_sync_jobs")
    assert len(jobs) == 1 and jobs[0]["mode"] == "manual" and jobs[0]["self_id"] == 88
    processor.router.actions.call.assert_not_awaited()
    assert "已创建同步任务" in (await repository.query("SELECT text FROM private_outbox"))[0]["text"]


async def test_sync_whitelist_ignore_identity_and_coverage(processor, repository):
    for text in ("/sync 999", "/sync all-history"):
        await processor.router.dispatch(command(text))
    await processor.router.dispatch(command("/sync", self_id=222))
    await set_policy(repository, "ignore")
    await processor.router.dispatch(command("/sync"))
    assert await repository.query("SELECT * FROM history_sync_jobs") == []
    await processor.router.dispatch(command("/coverage"))
    text = (await repository.query("SELECT text FROM private_outbox ORDER BY id DESC"))[0]["text"]
    assert "Collection Coverage" in text and "未确认缺口" in text and "最近周期核验" in text
