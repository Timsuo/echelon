"""Single-sender scheduling; SQLite remains the durable source of truth."""
import time

from app.notifier.priority import AGING_CAP, AGING_SECONDS, OutboxPriority

# Legacy unkeyed rows are independent ready rows; their lost chunk identity
# cannot be reconstructed from text. All new producers have an identity.
PREDECESSOR = """(o.producer_key IS NULL OR NOT EXISTS (
    SELECT 1 FROM private_outbox p WHERE p.self_id IS o.self_id AND p.sent_at IS NULL
      AND p.producer_kind IS o.producer_kind AND p.producer_key=o.producer_key
      AND p.producer_chunk<o.producer_chunk))"""
READY_SQL = f"""WITH ready AS (
    SELECT * FROM private_outbox WHERE self_id=? AND next_attempt<=?
      AND sent_at IS NULL AND cancelled_at IS NULL AND dead_letter_at IS NULL
    UNION ALL
    SELECT * FROM private_outbox WHERE self_id IS NULL AND next_attempt<=?
      AND sent_at IS NULL AND cancelled_at IS NULL AND dead_letter_at IS NULL)
    SELECT o.* FROM ready o WHERE {PREDECESSOR}
    ORDER BY MIN(?, o.priority + MIN(?, MAX(0, CAST((?-o.created_at)/? AS INTEGER)))) DESC,
    o.id ASC LIMIT 1"""


def ready_args(self_id, now):
    return (self_id, now, now, int(OutboxPriority.CRITICAL), AGING_CAP, now, AGING_SECONDS)


async def next_notification(repository, now=None):
    now = time.time() if now is None else now
    sid = int(await repository.state('onebot_self_id') or 0)
    if not sid:
        return None
    rows = await repository.query(READY_SQL, ready_args(sid, now))
    return rows[0] if rows else None


async def reconcile_failures(connection, self_id, now):
    for table, producer in (('inbox_deliveries', 'delivery'), ('digest_runs', 'digest')):
        # failed_at is the effective terminal state; status remains enqueued only
        # for compatibility with the original CHECK. All readers must apply overlay.
        await connection.execute(f"""UPDATE {table} SET failed_at=?,failure_reason=(
            SELECT COALESCE(o.error,'OutboxCancelled') FROM private_outbox o
            WHERE o.self_id={table}.self_id AND o.producer_kind=? AND o.producer_key=CAST({table}.id AS TEXT)
              AND (o.dead_letter_at IS NOT NULL OR o.cancelled_at IS NOT NULL) ORDER BY o.id LIMIT 1)
            WHERE self_id=? AND status='enqueued' AND failed_at IS NULL AND EXISTS (
            SELECT 1 FROM private_outbox o WHERE o.self_id={table}.self_id AND o.producer_kind=?
              AND o.producer_key=CAST({table}.id AS TEXT) AND (o.dead_letter_at IS NOT NULL OR o.cancelled_at IS NOT NULL))""",
            (now, producer, self_id, producer))


async def notification_result(repository, item, error=None, *, transient=False):
    now, config = time.time(), repository.outbox_config
    async with repository.db.transaction() as connection:
        async with connection.execute('SELECT * FROM private_outbox WHERE id=? AND self_id IS ?', (item['id'], item.get('self_id'))) as cursor:
            current = await cursor.fetchone()
        if not current or current['sent_at'] is not None or current['cancelled_at'] is not None or current['dead_letter_at'] is not None:
            return
        if error is None:
            await connection.execute('UPDATE private_outbox SET sent_at=?,error=NULL WHERE id=?', (now, item['id']))
            return
        # Only exception class identifiers are persisted, never exception messages.
        error = error if error.isascii() and error.isidentifier() and len(error) <= 80 else 'NotificationError'
        transient = transient or error in {'ConnectionError', 'TimeoutError'}
        terminal = current['terminal_attempts'] + (not transient)
        delay = min(config.retry_max_seconds, config.retry_base_seconds * 2**min(current['attempts'], 16))
        dead = not transient and terminal >= config.terminal_retry_limit
        await connection.execute('UPDATE private_outbox SET attempts=attempts+1,terminal_attempts=?,error=?,next_attempt=?,dead_letter_at=? WHERE id=?',
            (terminal, error, now+delay, now if dead else None, item['id']))
        if dead and current['producer_key'] is not None:
            await connection.execute("UPDATE private_outbox SET dead_letter_at=?,error='producer_chunk_failed' "
                'WHERE self_id IS ? AND producer_kind IS ? AND producer_key=? AND sent_at IS NULL '
                'AND cancelled_at IS NULL AND dead_letter_at IS NULL',
                (now, current['self_id'], current['producer_kind'], current['producer_key']))
        if dead:
            await reconcile_failures(connection, current['self_id'], now)
