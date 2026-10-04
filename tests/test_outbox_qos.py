import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from app.delivery.repository import DeliveryRepository
from app.notifier.priority import OutboxPriority as P
from app.notifier.qq import deliver_notification
from app.onebot.actions import ActionRejectedError
from app.storage.outbox_repository import READY_SQL, ready_args
from tests.test_delivery import outbox, preferences, revision
from tests.test_hierarchical_summary import generate


async def enqueue(repository, key, priority=P.SUMMARY, text='test'):
    async with repository.db.transaction() as connection:
        await repository.enqueue_text(connection, text, 88, producer_kind='summary', producer_key=key, priority=priority)
    return (await repository.query('SELECT * FROM private_outbox WHERE producer_key=? ORDER BY producer_chunk', (key,)))[0]


@pytest.mark.parametrize('priority', [P.CRITICAL, P.HIGH, P.INTERACTIVE])
async def test_delayed_summary_does_not_block_ready(priority, repository):
    older = await enqueue(repository, 'old', text='x'*4000)
    await repository.notification_result(older, 'ConnectionError')
    await enqueue(repository, 'new', priority)
    assert (await repository.next_notification())['producer_key'] == 'new'


async def test_legacy_delayed_row_does_not_block_other_ready_rows(repository):
    await repository.query("INSERT INTO private_outbox(text,created_at,next_attempt) VALUES ('legacy retry',1,?)", (time.time()+300,))
    await repository.query("INSERT INTO private_outbox(text,created_at) VALUES ('legacy ready',2)")
    assert (await repository.next_notification())['text'] == 'legacy ready'


async def test_priority_and_fifo(repository):
    for priority in P:
        for i in range(2):
            await enqueue(repository, f'{priority}-{i}', priority)
    selected = []
    while row := await repository.next_notification():
        selected.append((row['priority'], row['id']))
        await repository.notification_result(row)
    assert selected == sorted(selected, key=lambda r: (-r[0], r[1]))


async def test_preemption_between_chunks_preserves_intra_producer_order(repository):
    await enqueue(repository, 'a', text='x'*5000)
    first = await repository.next_notification()
    assert first['producer_chunk'] == 1
    await repository.notification_result(first)
    await enqueue(repository, 'urgent', P.CRITICAL)
    urgent = await repository.next_notification()
    assert urgent['producer_key'] == 'urgent'
    await repository.notification_result(urgent)
    second = await repository.next_notification()
    assert second['producer_chunk'] == 2
    await repository.notification_result(second, 'TimeoutError')
    assert await repository.next_notification() is None  # chunk 3 cannot pass retrying 2
    await repository.query('UPDATE private_outbox SET next_attempt=0')
    await repository.notification_result(await repository.next_notification())
    assert (await repository.next_notification())['producer_chunk'] == 3


async def test_aging_prevents_starvation_under_continuous_critical(repository):
    old = await enqueue(repository, 'old', P.BACKGROUND)
    now = time.time()
    await repository.query('UPDATE private_outbox SET created_at=? WHERE id=?', (now-3601, old['id']))
    for i in range(20):
        await enqueue(repository, str(i), P.CRITICAL)
    assert (await repository.next_notification(now))['id'] == old['id']


@pytest.mark.parametrize('error', [ConnectionError, TimeoutError])
async def test_transient_outbox_retry_survives_budget_and_restart(error, repository):
    await enqueue(repository, 'network')
    actions = AsyncMock()
    actions.call.side_effect = error('secret provider body')
    for _ in range(6):
        await repository.query('UPDATE private_outbox SET next_attempt=0')
        await deliver_notification(repository, actions, 99, await repository.next_notification())
    await repository.db.close()
    await repository.db.open()
    row = (await repository.query('SELECT * FROM private_outbox'))[0]
    assert row['attempts'] == 6 and row['terminal_attempts'] == 0 and row['dead_letter_at'] is None
    assert row['error'] == error.__name__ and row['next_attempt'] > time.time()
    await enqueue(repository, 'critical', P.CRITICAL)
    assert (await repository.next_notification())['producer_key'] == 'critical'


@pytest.mark.parametrize('error', [ActionRejectedError, PermissionError, ValueError])
async def test_terminal_budget_and_producer_failure_survive_restart(error, repository):
    await enqueue(repository, 'multi', text='x'*5000)
    await repository.notification_result(await repository.next_notification())  # chunk 1 delivered
    actions = AsyncMock()
    actions.call.side_effect = error('raw secret')
    for i in range(3):
        await repository.query('UPDATE private_outbox SET next_attempt=0')
        await deliver_notification(repository, actions, 99, await repository.next_notification())
    assert await repository.next_notification() is None
    await repository.db.close()
    await repository.db.open()
    rows = await repository.query('SELECT * FROM outbox_status ORDER BY id')
    assert rows[0]['effective_status'] == 'sent'
    assert rows[1]['effective_status'] == rows[2]['effective_status'] == 'dead_letter'
    assert rows[1]['attempts'] == 3 and rows[1]['error'] == error.__name__
    assert rows[2]['error'] == 'producer_chunk_failed'


async def test_transient_attempts_do_not_consume_terminal_budget(repository):
    item = await enqueue(repository, 'mixed')
    for _ in range(8):
        await repository.notification_result(item, 'ConnectionError')
    await repository.notification_result(item, 'ActionRejectedError')
    row = (await repository.query('SELECT * FROM private_outbox'))[0]
    assert row['terminal_attempts'] == 1 and row['dead_letter_at'] is None


@pytest.mark.parametrize('kind', ['delivery', 'digest'])
async def test_dead_letter_reconciles_and_retains_business_data(kind, repository, processor, config):
    await preferences(repository, urgent_enabled=kind == 'delivery')
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    if kind == 'digest':
        await delivery.manual(88, 'm')
    await delivery.tick(88)
    item = (await outbox(repository, kind))[0]
    for _ in range(3):
        await repository.notification_result(item, 'ActionRejectedError')
    table = 'inbox_delivery_status' if kind == 'delivery' else 'digest_run_status'
    state = (await repository.query(f'SELECT * FROM {table}'))[0]
    assert state['effective_status'] == 'failed' and state['failure_reason'] == 'ActionRejectedError'
    assert len(await repository.query('SELECT * FROM inbox_items')) == 1
    await repository.db.close()
    await repository.db.open()
    await delivery.tick(88)
    assert len(await outbox(repository, kind)) == 1
    assert (await repository.query(f'SELECT * FROM {table}'))[0]['effective_status'] == 'failed'


async def test_dead_letter_does_not_delete_saved_summary(repository, processor, config):
    saved, _ = await generate(repository, processor, config)
    for row in await outbox(repository, 'summary'):
        for _ in range(3):
            await repository.notification_result(row, 'ActionRejectedError')
    assert (await repository.query('SELECT * FROM summaries'))[0] == saved


async def test_dead_letter_retains_attachment_record_and_file(repository, processor, tmp_path):
    from tests.policy_helpers import set_policy
    from tests.test_inbox_files import downloaded
    await set_policy(repository)
    await downloaded(processor, repository, tmp_path)
    before = await repository.query('SELECT * FROM attachments')
    from pathlib import Path
    path = Path(before[0]['local_path'])
    content = path.read_bytes()
    notice = await enqueue(repository, 'attachment-notice')
    for _ in range(3):
        await repository.notification_result(notice, 'ActionRejectedError')
    assert await repository.query('SELECT * FROM attachments') == before
    assert path.read_bytes() == content


async def test_only_all_chunks_sent_can_reconcile_delivered(repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88)
    row = (await outbox(repository))[0]
    await repository.query('INSERT INTO private_outbox(text,created_at,self_id,producer_kind,producer_key,producer_chunk,priority) VALUES (?,?,88,?,?,2,?)',
        ('second chunk', time.time(), row['producer_kind'], row['producer_key'], row['priority']))
    await repository.notification_result(row)
    await delivery.tick(88)
    assert (await repository.query('SELECT effective_status FROM inbox_delivery_status'))[0]['effective_status'] == 'enqueued'
    second = (await outbox(repository))[1]
    await repository.notification_result(second)
    await repository.db.close()
    await repository.db.open()
    await delivery.tick(88)
    assert (await repository.query('SELECT effective_status FROM inbox_delivery_status'))[0]['effective_status'] == 'delivered'
    assert len(await outbox(repository)) == 2


async def test_send_ack_then_crash_before_sent_at_is_recoverable(repository, monkeypatch):
    await enqueue(repository, 'crash')
    row = await repository.next_notification()
    actions = AsyncMock()
    with monkeypatch.context() as patch:
        patch.setattr(repository, 'notification_result', AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await deliver_notification(repository, actions, 99, row)
    actions.call.assert_awaited_once()
    await repository.db.close()
    await repository.db.open()
    assert (await repository.next_notification())['id'] == row['id']
    await deliver_notification(repository, actions, 99, await repository.next_notification())
    assert actions.call.await_count == 2  # Explicitly NOT QQ end-to-end exactly-once.
    assert len(await repository.query('SELECT * FROM private_outbox')) == 1


async def test_synthetic_1000_rows_ready_plan_and_latency(repository):
    now = time.time()
    async with repository.db.transaction() as connection:
        await connection.executemany('INSERT INTO private_outbox(text,created_at,self_id,priority,next_attempt,producer_kind,producer_key,producer_chunk,dead_letter_at) VALUES (?,?,?,?,?,?,?,?,?)',
            [('synthetic', now, 88, int(P.SUMMARY), now+300 if i%3 else 0, 'summary', str(i//3), i%3+1, now if i%11 == 0 else None) for i in range(1002)])
    await enqueue(repository, 'critical', P.CRITICAL)
    start = time.perf_counter()
    assert (await repository.next_notification())['producer_key'] == 'critical'
    assert time.perf_counter()-start < 2  # Generous regression ceiling, not microbenchmark.
    plan = await repository.query('EXPLAIN QUERY PLAN '+READY_SQL, ready_args(88, now))
    description = '\n'.join(row['detail'] for row in plan)
    assert 'outbox_ready' in description and 'outbox_predecessor' in description
    # The final CTE result may be scanned/sorted for aging, but both underlying
    # table arms must search only this account's due rows, not scan the table.
    assert description.count('SEARCH private_outbox USING INDEX outbox_ready (self_id=? AND next_attempt<?)') == 2


async def test_revoke_does_not_revive_dead_letter_or_new_delivery(repository, processor, config):
    await preferences(repository)
    await revision(repository, processor, config)
    delivery = DeliveryRepository(repository, config)
    await delivery.tick(88)
    row = (await outbox(repository))[0]
    await repository.notification_result(row, 'ConnectionError')
    await repository.authorizations.deactivate(88, 123)
    for _ in range(3):
        await repository.notification_result(row, 'ActionRejectedError')
    await repository.authorizations.activate(88, 123)
    await delivery.tick(88)
    assert len(await outbox(repository)) == 1
    assert (await outbox(repository))[0]['dead_letter_at'] is not None
