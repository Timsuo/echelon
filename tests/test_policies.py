import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.config import DeepSeekConfig
from app.jobs.service import SummaryService
from app.llm.deepseek import RetryableModelOutputError
from app.policies.models import ConfigIntent, GroupPolicy, PolicyChanges
from app.policies.parser import ConfigIntentParser
from app.policies.worker import ConfigurationWorker
from app.storage.policy_repository import PolicyRepository
from tests.conftest import event
from tests.policy_helpers import set_policy
from tests.test_attachments import build_worker, file_event
from tests.test_deepseek import completion
from tests.test_inbox_files import command
from tests.test_model_output import mock_client


def intent(**changes):
    return ConfigIntent(action="update_group_policy", changes=PolicyChanges(**changes), reason="测试配置意图")


@pytest.fixture
def policies(repository):
    return PolicyRepository(repository.db, [123])


async def test_default_policy_summary_only_metadata_without_inbox(processor, repository, policies):
    policy = await policies.get(88, 123)
    assert policy.mode == "summary_only" and policy.summary_enabled
    assert not policy.inbox_enabled and not policy.attachment_download_enabled and not policy.priority_watch_enabled
    await processor.handle(file_event())
    assert len(await repository.query("SELECT * FROM messages")) == 1
    row = (await repository.query("SELECT * FROM attachments"))[0]
    assert row["download_status"] == "skipped" and row["error"] == "group_policy"
    assert await repository.query("SELECT * FROM inbox_items") == []


@pytest.mark.parametrize("mode", ["inbox", "priority"])
async def test_inbox_profiles_create_items(mode, processor, repository, policies):
    await set_policy(repository, mode)
    await processor.handle(file_event())
    assert len(await repository.query("SELECT * FROM inbox_items")) == 1
    assert (await repository.query("SELECT * FROM attachments"))[0]["download_status"] == "pending"
    assert (await policies.get(88, 123)).priority_watch_enabled is (mode == "priority")


async def test_ignore_skips_whole_business_pipeline(processor, repository, caplog):
    await set_policy(repository, "ignore")
    with caplog.at_level("INFO"):
        await processor.handle(file_event())
        await processor.handle(event(message_id=2))
    for table in ("messages", "attachments", "inbox_items"):
        assert await repository.query(f"SELECT * FROM {table}") == []
    assert "Group ignored by policy" in caplog.text


async def test_summary_only_can_download_without_inbox(processor, repository):
    await set_policy(repository, "summary_only", attachment_download_enabled=True)
    await processor.handle(file_event())
    assert (await repository.query("SELECT * FROM attachments"))[0]["download_status"] == "pending"
    assert await repository.query("SELECT * FROM inbox_items") == []


async def test_inbox_can_disable_download_without_changing_mode(processor, repository, policies):
    await set_policy(repository, "inbox", attachment_download_enabled=False)
    await processor.handle(file_event())
    assert len(await repository.query("SELECT * FROM inbox_items")) == 1
    assert (await repository.query("SELECT * FROM attachments"))[0]["download_status"] == "skipped"
    assert (await policies.get(88, 123)).mode == "inbox"


async def test_worker_rechecks_policy_after_queue(processor, repository, tmp_path):
    await set_policy(repository)
    await processor.handle(file_event())
    worker, client, _ = build_worker(repository, tmp_path, lambda request: pytest.fail("Must not download"))
    try:
        row = await worker.repository.claim_attachment(88)
        await set_policy(repository, "summary_only")
        await worker.process(row)
        worker.resolver.resolve.assert_not_awaited()
        assert (await worker.repository.attachment(88, row["id"]))["download_status"] == "skipped"
    finally:
        await client.aclose()


async def test_policy_self_id_isolation(repository, policies):
    await set_policy(repository, "priority", self_id=222, alias="其他账号")
    assert (await policies.get(88, 123)).mode == "summary_only"
    assert (await policies.get(222, 123)).mode == "priority"
    with pytest.raises(ValueError):
        ConfigIntentParser.target("其他账号需要重点关注", await policies.listing(88))


def test_duplicate_alias_requires_clarification():
    rows = [GroupPolicy(self_id=88, group_id=i, alias="课程群") for i in (123, 456)]
    with pytest.raises(ValueError, match=r"123.*课程群[\s\S]*456"):
        ConfigIntentParser.target("把课程群设置为 priority", rows)
    assert ConfigIntentParser.target("123 mode priority", rows) == 123


@pytest.mark.parametrize("text", ["999 mode priority", "这个群比较重要", "123 456 mode inbox"])
def test_unknown_or_ambiguous_target_never_guessed(text):
    if text == "123 456 mode inbox":
        # Phase 2.5: only the explicit first token is a target; body numbers are ordinary data.
        assert ConfigIntentParser.target(text, [GroupPolicy(self_id=88, group_id=123)]) == 123
        return
    with pytest.raises(ValueError):
        ConfigIntentParser.target(text, [GroupPolicy(self_id=88, group_id=123)])


@pytest.mark.parametrize("payload", [
    {}, {"action": "SQL", "changes": {"mode": "inbox"}, "reason": "bad"},
    {"action": "update_group_policy", "group_id": 999, "changes": {"mode": "inbox"}, "reason": "bad"},
    {"action": "update_group_policy", "changes": {"shell": "whoami"}, "reason": "bad"},
    {"action": "update_group_policy", "changes": {"summary_enabled": "true"}, "reason": "bad"},
    {"action": "update_group_policy", "changes": {"mode": None}, "reason": "bad"},
    {"action": "update_group_policy", "changes": {}, "reason": "bad"},
])
def test_config_intent_strict_schema(payload):
    with pytest.raises(ValidationError):
        ConfigIntent.model_validate(payload)


async def test_proposal_confirm_exact_diff_and_idempotent(repository, policies):
    before = await set_policy(repository, "priority", alias="高数群")
    identifier = await policies.propose(88, 99, 123, intent(attachment_download_enabled=False))
    assert await policies.get(88, 123) == before
    proposal = (await repository.query("SELECT * FROM configuration_proposals"))[0]
    data = json.loads(proposal["intent_json"])
    assert data["after"] == {"attachment_download_enabled": False}
    assert data["before"] == {"attachment_download_enabled": True}
    assert "True → False" in (await repository.query("SELECT text FROM private_outbox"))[0]["text"]
    assert await policies.resolve(88, 99, identifier, True) == "配置已更新。"
    after = await policies.get(88, 123)
    assert after.model_dump() == before.model_dump() | {"attachment_download_enabled": False}
    assert "confirmed" in await policies.resolve(88, 99, identifier, True)
    assert await policies.get(88, 123) == after


async def test_cancel_and_expiry_do_not_change_policy(repository, policies):
    first = await policies.propose(88, 99, 123, intent(mode="inbox"))
    await policies.resolve(88, 99, first, False)
    assert "cancelled" in await policies.resolve(88, 99, first, True)
    second = await policies.propose(88, 99, 123, intent(mode="priority"))
    await repository.query("UPDATE configuration_proposals SET expires_at=0 WHERE id=?", (second,))
    assert "过期" in await policies.resolve(88, 99, second, True)
    assert (await policies.get(88, 123)).mode == "summary_only"
    assert (await repository.query("SELECT status FROM configuration_proposals WHERE id=?", (second,)))[0]["status"] == "expired"


async def test_expiry_sweeper_and_stale_diff(repository, policies):
    identifier = await policies.propose(88, 99, 123, intent(mode="inbox"))
    await set_policy(repository, "priority")
    assert "失效" in await policies.resolve(88, 99, identifier, True)
    assert (await policies.get(88, 123)).mode == "priority"
    identifier = await policies.propose(88, 99, 123, intent(mode="inbox"))
    await repository.query("UPDATE configuration_proposals SET expires_at=0 WHERE id=?", (identifier,))
    await policies.claim()
    assert (await repository.query("SELECT status FROM configuration_proposals WHERE id=?", (identifier,)))[0]["status"] == "expired"


async def test_proposal_account_admin_and_allowlist_guards(repository, policies):
    identifier = await policies.propose(88, 99, 123, intent(mode="inbox"))
    for self_id, admin in ((222, 99), (88, 100)):
        with pytest.raises(ValueError):
            await policies.resolve(self_id, admin, identifier, True)
    await repository.authorizations.deactivate(88, 123)
    with pytest.raises(ValueError):
        await policies.resolve(88, 99, identifier, True)
    await repository.authorizations.activate(88, 123)
    with pytest.raises(ValueError):
        await policies.propose(88, 99, 999, intent(mode="inbox"))
    assert (await policies.get(88, 123)).mode == "summary_only"


async def test_commands_groups_group_local_config_confirm_cancel(processor, repository, policies):
    for text in ("/groups", "/group 123", "/config 123 mode inbox"):
        await processor.router.dispatch(command(text))
    assert await repository.query("SELECT * FROM configuration_requests") == []
    texts = "\n".join(row["text"] for row in await repository.query("SELECT text FROM private_outbox"))
    assert "Echelon Groups" in texts and "Group Policy" in texts and "summary_only → inbox" in texts
    assert (await policies.get(88, 123)).mode == "summary_only"
    await processor.router.dispatch(command("/confirm 1"))
    assert (await policies.get(88, 123)).mode == "inbox"
    await processor.router.dispatch(command("/config 123 alias 高数群"))
    await processor.router.dispatch(command("/cancel 2"))
    assert (await policies.get(88, 123)).alias is None


async def test_nonadmin_and_plain_private_text_cannot_configure(processor, repository):
    await processor.router.dispatch(command("/config 123 mode inbox", user_id=100))
    await processor.router.dispatch(command("123 mode inbox"))
    assert await repository.query("SELECT * FROM configuration_proposals") == []
    assert await repository.query("SELECT * FROM configuration_requests") == []
    assert await repository.query("SELECT * FROM private_outbox") == []


@pytest.mark.parametrize("text,changes", [
    ("123 以后叫高数群，并作为课程收件箱处理", {"alias": "高数群", "mode": "inbox"}),
    ("音游群平时主要闲聊，只需要总结，不要放进收件箱", {"mode": "summary_only"}),
    ("班级群需要重点关注", {"mode": "priority"}),
])
async def test_natural_language_scenarios_propose_then_confirm(text, changes, processor, repository, policies):
    await set_policy(repository, "inbox", alias="音游群" if "音游群" in text else "班级群")
    before = await policies.get(88, 123)
    await processor.router.dispatch(command("/config " + text))
    request = await policies.claim()
    assert request is not None
    llm = AsyncMock()
    llm.parse_config.return_value = intent(**changes)
    await ConfigurationWorker(policies, llm).execute(request)
    assert await policies.get(88, 123) == before
    proposal = (await repository.query("SELECT * FROM configuration_proposals"))[0]
    await processor.router.dispatch(command(f"/confirm {proposal['id']}"))
    after = await policies.get(88, 123)
    assert after.mode == changes["mode"]
    if "alias" in changes:
        assert after.alias == changes["alias"]
    if changes["mode"] == "priority":
        assert after.priority_watch_enabled
        assert any("Priority Watch" in row["text"] for row in await repository.query("SELECT text FROM private_outbox"))


async def test_natural_file_toggle_is_local_minimal_change(processor, repository, policies):
    before = await set_policy(repository, "priority", alias="高数群")
    await processor.router.dispatch(command("/config 高数群不要自动下载文件"))
    assert await repository.query("SELECT * FROM configuration_requests") == []
    await processor.router.dispatch(command("/confirm 1"))
    assert (await policies.get(88, 123)).model_dump() == before.model_dump() | {"attachment_download_enabled": False}


def test_model_cannot_add_unrequested_fields():
    for changes in ({"mode": "summary_only"}, {"priority_watch_enabled": False}, {"alias": "new"}):
        with pytest.raises(ValueError):
            ConfigIntentParser.validate_intent(intent(**changes), "高数群不要自动下载文件")


async def test_config_llm_strict_schema_and_no_invented_group():
    bad = intent(mode="inbox").model_dump(exclude_unset=True) | {"group_id": 999}
    client, requests = mock_client([completion(json.dumps(bad))], DeepSeekConfig(retries=0))
    try:
        with pytest.raises(RetryableModelOutputError):
            await client.parse_config("123 作为课程收件箱", AsyncMock())
        assert len(requests) == 1
        assert requests[0]["thinking"] == {"type": "disabled"}
        assert "不能扩大采集白名单" in requests[0]["messages"][0]["content"]
    finally:
        await client.close()


async def test_config_llm_valid_response():
    result = intent(mode="inbox")
    client, requests = mock_client([completion(result.model_dump_json(exclude_unset=True))])
    try:
        assert await client.parse_config("123 作为课程收件箱", AsyncMock()) == result
        assert requests[0]["response_format"] == {"type": "json_object"}
    finally:
        await client.close()


async def test_configuration_failure_and_restart_recovery(repository, policies):
    await policies.queue(88, 99, 123, "cmd", "123 作为课程收件箱")
    await policies.claim()
    await policies.recover()
    request = await policies.claim()
    llm = AsyncMock()
    llm.parse_config.side_effect = RuntimeError("private provider details")
    await ConfigurationWorker(policies, llm).execute(request)
    assert (await repository.query("SELECT * FROM configuration_requests"))[0]["status"] == "failed"
    assert await repository.query("SELECT * FROM configuration_proposals") == []
    assert (await policies.get(88, 123)).mode == "summary_only"
    assert all("private provider" not in row["text"] for row in await repository.query("SELECT text FROM private_outbox"))


async def test_summary_policy_checked_at_enqueue_and_execution(processor, repository, policies, config):
    await processor.router.dispatch(command("/summary 2h"))
    job = await repository.claim_job()
    assert job is not None
    await set_policy(repository, "inbox", summary_enabled=False)
    await processor.router.dispatch(command("/summary 30m"))
    assert len(await repository.query("SELECT * FROM summary_jobs")) == 1
    llm = AsyncMock()
    await SummaryService(repository, llm, config).execute(job)
    llm.summarize.assert_not_awaited()
    assert (await repository.query("SELECT * FROM summary_jobs"))[0]["status"] == "failed"
    await processor.router.dispatch(command("/status"))
    assert "DeepSeek" in (await repository.query("SELECT text FROM private_outbox ORDER BY id DESC"))[0]["text"]


async def test_policy_migration_retains_existing_policies_proposals_and_requests(repository, policies):
    original = await set_policy(repository, "priority", alias="班级群")
    identifier = await policies.propose(88, 99, 123, intent(attachment_download_enabled=False))
    await policies.queue(88, 99, 123, "queued", "123 只做总结")
    for _ in range(2):
        await repository.db.close()
        await repository.db.open()
        assert await policies.get(88, 123) == original
        assert len(await repository.query("SELECT * FROM configuration_proposals")) == 1
        assert len(await repository.query("SELECT * FROM configuration_requests")) == 1
    await policies.resolve(88, 99, identifier, True)
    assert not (await policies.get(88, 123)).attachment_download_enabled


async def test_config_request_queue_bound_to_account(repository, policies):
    await policies.queue(88, 99, 123, "queued", "123 作为课程收件箱")
    await policies.queue(88, 99, 123, "queued", "123 作为课程收件箱")
    assert len(await repository.query("SELECT * FROM configuration_requests")) == 1
    await repository.state("onebot_self_id", "222")
    assert await policies.claim() is None
    await repository.state("onebot_self_id", "88")
    assert (await policies.claim())["self_id"] == 88
