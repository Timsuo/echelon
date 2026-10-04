import hashlib
import json
import time
import unicodedata

from app.delivery.preferences import read_delivery_preferences
from app.delivery.renderer import render_alert, render_digest
from app.delivery.schedule import quiet_end, slots
from app.notifier.priority import OutboxPriority
from app.storage.authorization_repository import active_ids, is_active
from app.storage.history_repository import HistoryRepository, rows
from app.storage.outbox_repository import reconcile_failures
from app.storage.policy_repository import read_policy
from app.storage.preference_repository import read_preferences
from app.storage.repository import Repository


def fingerprint(item: dict) -> str:
    def normalize(value):
        return ''.join(c.casefold() for c in unicodedata.normalize('NFKC', value or '')
                       if not unicodedata.category(c).startswith(('P', 'Z')) and not c.isspace())
    material = {key: item.get(key) for key in ('priority', 'category', 'deadline_at', 'action_required')}
    for key in ('title', 'action_text'):
        material[key] = normalize(item.get(key))
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def provenance(connection, self_id, item_id, revision):
    found = await rows(connection, 'SELECT e.*,j.source_kind,j.window_start,j.window_end,MAX(m.event_time) AS event_time '
        'FROM inbox_revision_events e JOIN triage_jobs j ON j.id=e.triage_job_id AND j.self_id=e.self_id '
        'JOIN triage_job_messages l ON l.job_id=j.id AND l.self_id=e.self_id '
        'JOIN messages m ON m.id=l.message_id AND m.self_id=e.self_id '
        'JOIN inbox_item_messages im ON im.message_id=m.id AND im.inbox_item_id=e.inbox_item_id AND im.self_id=e.self_id '
        'WHERE e.self_id=? AND e.inbox_item_id=? AND e.revision=? GROUP BY e.id', (self_id, item_id, revision))
    return found[0] if found else None


def relevant(item, origin, prefs, now):
    deadline = item['deadline_at']
    if deadline is not None and deadline <= now:
        return False
    if origin['source_kind'] == 'realtime':
        return True
    if not prefs.recovery_alert_enabled:
        return False
    actionable = bool(item['action_required'] or (deadline is not None and deadline > now))
    recent = 0 <= now - origin['event_time'] <= prefs.recovery_alert_max_age_hours * 3600
    # Without a known future time, an old schedule cannot be assumed actionable.
    return actionable and (recent or (deadline is not None and deadline > now))


async def eligible(connection, item, prefs, origin, now):
    if not prefs.enabled or not prefs.urgent_enabled or item['status'] == 'archived' or item['priority'] not in prefs.urgent_priorities:
        return False
    if not await is_active(connection, item['self_id'], item['source_group_id']):
        return False
    policy = await read_policy(connection, item['self_id'], item['source_group_id'])
    return policy.mode != 'ignore' and policy.priority_watch_enabled and origin is not None and relevant(item, origin, prefs, now)


class DeliveryRepository:
    def __init__(self, repository, config):
        self.repository, self.db, self.config = repository, repository.db, config

    async def reconcile(self, connection, self_id, now):
        await reconcile_failures(connection, self_id, now)
        for table, producer in (('inbox_deliveries', 'delivery'), ('digest_runs', 'digest')):
            await connection.execute(f"UPDATE {table} SET status='delivered',delivered_at=? WHERE self_id=? AND status='enqueued' AND failed_at IS NULL "
                "AND EXISTS (SELECT 1 FROM private_outbox o WHERE o.self_id=? AND o.producer_kind=? AND o.producer_key=CAST("+table+".id AS TEXT)) "
                "AND NOT EXISTS (SELECT 1 FROM private_outbox o WHERE o.self_id=? AND o.producer_kind=? AND o.producer_key=CAST("+table+".id AS TEXT) AND (o.sent_at IS NULL OR o.cancelled_at IS NOT NULL))",
                (now, self_id, self_id, producer, self_id, producer))

    async def fast_track(self, connection, self_id, prefs, now):
        if not prefs.enabled or not prefs.fast_track_enabled or not self.config.triage.enabled:
            return
        personal = await read_preferences(connection, self_id)
        words = [*personal.important_keywords, '紧急', '立即', '今天', '截止', '调课']
        pending = await rows(connection, "SELECT s.message_id,s.group_id,s.created_at,m.normalized_text,m.user_id "
            "FROM triage_message_state s JOIN messages m ON m.id=s.message_id AND m.self_id=s.self_id "
            "WHERE s.self_id=? AND s.status='pending' AND s.fast_track_at IS NULL", (self_id,))
        for message in pending:
            if not await is_active(connection, self_id, message['group_id']):
                continue
            if message['user_id'] in personal.important_senders or any(word.casefold() in message['normalized_text'].casefold() for word in words):
                await connection.execute('UPDATE triage_message_state SET fast_track_at=? WHERE message_id=? AND self_id=?',
                    (max(now, message['created_at'] + prefs.fast_track_debounce_seconds), message['message_id'], self_id))

    async def evaluate(self, connection, self_id, prefs, start, now):
        items = await rows(connection, 'SELECT i.*,e.created_at AS revision_at FROM inbox_revision_events e '
            'JOIN inbox_items i ON i.id=e.inbox_item_id AND i.self_id=e.self_id AND i.revision=e.revision '
            'WHERE e.self_id=? AND e.created_at>=? AND NOT EXISTS (SELECT 1 FROM delivery_revision_state s '
            'WHERE s.self_id=e.self_id AND s.inbox_item_id=e.inbox_item_id AND s.revision=e.revision) ORDER BY e.id LIMIT 200', (self_id, start))
        for item in items:
            origin = await provenance(connection, self_id, item['id'], item['revision'])
            key = fingerprint(item)
            decision = 'not_eligible'
            if await eligible(connection, item, prefs, origin, now):
                prior = await rows(connection, "SELECT * FROM inbox_deliveries WHERE self_id=? AND inbox_item_id=? AND status IN ('enqueued','delivered') AND failed_at IS NULL ORDER BY id DESC LIMIT 1", (self_id, item['id']))
                kind = 'recovery' if origin['source_kind'] != 'realtime' else ('urgent_update' if prior else 'urgent')
                until = quiet_end(prefs, now, self.config.timezone)
                override = getattr(prefs, item['priority']+'_break_quiet_hours')
                deferred = until is not None and not override
                decision = 'deferred' if deferred else 'queued'
                # A cancelled deferred revision may become eligible again later;
                # revive the same unsent fingerprint, never a sent fingerprint.
                await connection.execute('INSERT INTO inbox_deliveries(self_id,inbox_item_id,kind,item_revision,fingerprint,snapshot_json,status,scheduled_for,created_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(self_id,inbox_item_id,fingerprint) DO UPDATE SET '
                    'item_revision=excluded.item_revision,snapshot_json=excluded.snapshot_json,status=excluded.status,scheduled_for=excluded.scheduled_for,cancelled_at=NULL '
                    "WHERE inbox_deliveries.status='cancelled' AND inbox_deliveries.enqueued_at IS NULL",
                    (self_id, item['id'], kind, item['revision'], key, json.dumps(item, ensure_ascii=False), decision, until if deferred else now, now))
            await connection.execute('INSERT INTO delivery_revision_state VALUES (?,?,?,?,?,?)',
                (self_id, item['id'], item['revision'], key, now, decision))

    async def enqueue_due(self, connection, self_id, prefs, now):
        pending = await rows(connection, "SELECT * FROM inbox_deliveries WHERE self_id=? AND status IN ('queued','deferred') ORDER BY id", (self_id,))
        for delivery in pending:
            found = await rows(connection, 'SELECT * FROM inbox_items WHERE id=? AND self_id=?', (delivery['inbox_item_id'], self_id))
            item = found[0] if found else None
            origin = await provenance(connection, self_id, item['id'], item['revision']) if item else None
            if not item or fingerprint(item) != delivery['fingerprint'] or not await eligible(connection, item, prefs, origin, now):
                await connection.execute("UPDATE inbox_deliveries SET status='cancelled',cancelled_at=? WHERE id=?", (now, delivery['id']))
                continue
            until = quiet_end(prefs, now, self.config.timezone)
            if until and not getattr(prefs, item['priority']+'_break_quiet_hours'):
                await connection.execute("UPDATE inbox_deliveries SET status='deferred',scheduled_for=? WHERE id=?", (until, delivery['id']))
                continue
            # Preference changes can end quiet hours early; re-evaluation is intentional.
            previous = await rows(connection, "SELECT snapshot_json FROM inbox_deliveries WHERE self_id=? AND inbox_item_id=? AND id!=? AND status IN ('enqueued','delivered') AND failed_at IS NULL ORDER BY id DESC LIMIT 1",
                                  (self_id, item['id'], delivery['id']))
            gaps = await HistoryRepository.unresolved_on(connection, self_id, item['source_group_id'], origin['window_start'], origin['window_end'])
            coverage = [g['coverage'] for g in gaps]
            if item['coverage_status'] == 'warning' and not coverage:
                coverage = ['unknown']
            text = render_alert(item, delivery['kind'], json.loads(previous[0]['snapshot_json']) if previous else None,
                                coverage, self.config.timezone, now, origin['event_time'])
            priority = (OutboxPriority.CRITICAL if item['priority'] == 'critical' else
                        OutboxPriority.RECOVERY if delivery['kind'] == 'recovery' else OutboxPriority.HIGH)
            await Repository.enqueue_text(connection, text, self_id, producer_kind='delivery', producer_key=str(delivery['id']), priority=priority)
            await connection.execute("UPDATE inbox_deliveries SET status='enqueued',enqueued_at=?,item_revision=?,snapshot_json=? WHERE id=?",
                (now, item['revision'], json.dumps(item, ensure_ascii=False), delivery['id']))

    async def schedule(self, connection, self_id, prefs, settings, now):
        # Old schedule slots outside the catchup horizon are acknowledged without
        # consuming Inbox revisions. One recent run coalesces all missed slots.
        start = max(settings['delivery_start_at'], settings['schedule_cursor'], now - prefs.digest_catchup_minutes*60)
        due = slots(prefs, start, now, self.config.timezone) if prefs.enabled and prefs.digest_enabled else []
        if due:
            kind = 'scheduled' if len(due) == 1 and now-due[-1] <= prefs.heartbeat_seconds else 'catchup'
            await self.create_digest_on(connection, self_id, kind, due[-1], now, settings['delivery_start_at'])
        await connection.execute('UPDATE delivery_settings SET schedule_cursor=?,last_heartbeat=? WHERE self_id=?', (now, now, self_id))

    async def create_digest_on(self, connection, self_id, kind, scheduled_for, now, start):
        previous = await rows(connection, "SELECT MAX(window_end) AS end FROM digest_runs WHERE self_id=? AND status IN ('delivered','skipped')", (self_id,))
        start = max(start, previous[0]['end'] or start)
        created = await rows(connection, 'INSERT INTO digest_runs(self_id,kind,scheduled_for,window_start,window_end,created_at) '
            'VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id', (self_id, kind, scheduled_for, start, now, now))
        return created[0]['id'] if created else None

    async def manual(self, self_id, message_id):
        now = time.time()
        async with self.db.transaction() as connection:
            settings = await self.settings(connection, self_id)
            # Per-event idempotency; a manual run uses the same Phase 4 revision window.
            receipt = await rows(connection, 'INSERT INTO command_receipts VALUES (?,?,?) ON CONFLICT DO NOTHING RETURNING message_id',
                                 (self_id, message_id, now))
            if not receipt:
                return None
            identifier = await self.create_digest_on(connection, self_id, 'manual', now, now, settings['delivery_start_at'])
            await Repository.enqueue_text(connection, f'已创建手动收信任务 #{identifier}。', self_id)
            self.db.delivery_wakeup.set()
            return identifier

    async def digest(self, connection, self_id, prefs, now):
        if prefs.enabled and prefs.digest_enabled and not quiet_end(prefs, now, self.config.timezone):
            waiting = await rows(connection, "SELECT * FROM digest_runs WHERE self_id=? AND kind!='manual' AND status IN ('queued','deferred') ORDER BY id", (self_id,))
            if len(waiting) > 1:
                latest = waiting[-1]
                await connection.execute("UPDATE digest_runs SET status='skipped',coverage_status='coalesced' WHERE self_id=? AND kind!='manual' AND status IN ('queued','deferred') AND id!=?", (self_id, latest['id']))
                await connection.execute("UPDATE digest_runs SET window_start=?,window_end=? WHERE id=?",
                    (min(r['window_start'] for r in waiting), max(r['window_end'] for r in waiting), latest['id']))
        runs = await rows(connection, "SELECT * FROM digest_runs WHERE self_id=? AND status IN ('queued','deferred') ORDER BY id", (self_id,))
        for run in runs:
            if run['kind'] != 'manual' and (not prefs.enabled or not prefs.digest_enabled or quiet_end(prefs, now, self.config.timezone)):
                await connection.execute("UPDATE digest_runs SET status='deferred' WHERE id=?", (run['id'],))
                continue
            items = await rows(connection, 'SELECT DISTINCT i.* FROM inbox_items i JOIN inbox_revision_events e '
                'ON e.inbox_item_id=i.id AND e.self_id=i.self_id AND e.revision=i.revision '
                "JOIN group_authorizations a ON a.self_id=i.self_id AND a.group_id=i.source_group_id AND a.active=1 "
                "WHERE i.self_id=? AND i.status!='archived' AND (? OR i.status!='read') AND e.created_at>=? AND e.created_at<=? "
                'AND NOT EXISTS (SELECT 1 FROM digest_run_items di JOIN digest_runs d ON d.id=di.digest_id '
                "WHERE di.self_id=i.self_id AND di.inbox_item_id=i.id AND di.item_revision=i.revision AND d.status IN ('enqueued','delivered')) ORDER BY i.id",
                (self_id, prefs.digest_include_read, run['window_start'], run['window_end']))
            items = [item for item in items if (await read_policy(connection, self_id, item['source_group_id'])).mode != 'ignore']
            warnings = []
            for group_id in await active_ids(connection, self_id):
                warnings.extend(await HistoryRepository.unresolved_on(connection, self_id, group_id, run['window_start'], run['window_end']))
            for item in items:
                urgent = await rows(connection, "SELECT 1 FROM inbox_deliveries WHERE self_id=? AND inbox_item_id=? AND fingerprint=? AND status IN ('enqueued','delivered') AND failed_at IS NULL",
                                    (self_id, item['id'], fingerprint(item)))
                item['was_urgent'] = bool(urgent)
                if item['coverage_status'] == 'warning':
                    warnings.append({'coverage': 'unknown'})
            overviews = await self.overviews(connection, self_id, run) if prefs.digest_include_group_summaries else []
            if not items and not overviews and not prefs.send_empty_digest:
                await connection.execute("UPDATE digest_runs SET status='skipped',coverage_status=? WHERE id=?",
                    ('warning' if warnings else 'no_known_gap', run['id']))
                continue
            for item in items:
                await connection.execute('INSERT INTO digest_run_items VALUES (?,?,?,?,?,?)',
                    (run['id'], self_id, item['id'], item['revision'], item['was_urgent'], json.dumps(item, ensure_ascii=False)))
            text = render_digest(run, items, overviews, warnings, self.config.timezone)
            # Selection, snapshots, outbox chunks and producer state commit together.
            await Repository.enqueue_text(connection, text, self_id, producer_kind='digest', producer_key=str(run['id']),
                priority=OutboxPriority.MANUAL_DIGEST if run['kind'] == 'manual' else OutboxPriority.SCHEDULED_DIGEST)
            await connection.execute("UPDATE digest_runs SET status='enqueued',enqueued_at=?,rendered_text=?,coverage_status=? WHERE id=?",
                (now, text, 'warning' if warnings else 'no_known_gap', run['id']))

    async def overviews(self, connection, self_id, run):
        from app.rendering.icons import plain
        found = await rows(connection, 'SELECT s.*,p.alias,a.group_name FROM summaries s '
            'JOIN group_authorizations a ON a.self_id=s.self_id AND a.group_id=s.group_id AND a.active=1 '
            'LEFT JOIN group_policies p ON p.self_id=s.self_id AND p.group_id=s.group_id '
            "WHERE s.self_id=? AND COALESCE(p.mode,'summary_only')='summary_only' AND s.created_at>=? AND s.created_at<=? "
            'ORDER BY s.created_at DESC,s.id DESC', (self_id, run['window_start'], run['window_end']))
        seen, overview = set(), []
        for summary in found:
            if summary['group_id'] in seen:
                continue
            seen.add(summary['group_id'])
            try:
                data = json.loads(summary['summary_json'])
                titles = [plain(t['title']) for t in data['topics'][:2]]
                headline = '、'.join(titles) or '本窗口没有已采集的新话题'
                overview.append(f"{summary['alias'] or summary['group_name'] or summary['group_id']}：{headline}\n{len(data['topics'])}个话题 · /summary detail {summary['id']}")
            except (ValueError, KeyError, TypeError):
                continue
        return overview

    async def settings(self, connection, self_id):
        bound = await rows(connection, "SELECT value FROM runtime_state WHERE key='onebot_self_id'")
        if not bound or bound[0]['value'] != str(self_id):
            raise ValueError('投递账号不匹配')
        await connection.execute('INSERT INTO delivery_settings SELECT ?,started_at,NULL,started_at FROM phase4_settings WHERE id=1 ON CONFLICT DO NOTHING', (self_id,))
        return (await rows(connection, 'SELECT * FROM delivery_settings WHERE self_id=?', (self_id,)))[0]

    async def tick(self, self_id, now=None):
        now = time.time() if now is None else now
        async with self.db.transaction() as connection:
            settings = await self.settings(connection, self_id)
            prefs = await read_delivery_preferences(connection, self_id)
            await self.reconcile(connection, self_id, now)
            await self.fast_track(connection, self_id, prefs, now)
            await self.evaluate(connection, self_id, prefs, settings['delivery_start_at'], now)
            await self.enqueue_due(connection, self_id, prefs, now)
            await self.schedule(connection, self_id, prefs, settings, now)
            await self.digest(connection, self_id, prefs, now)
            return prefs.heartbeat_seconds

    async def next_due(self, self_id, heartbeat_at):
        from app.delivery.schedule import next_digest
        now = time.time()
        async with self.db.transaction() as connection:
            prefs = await read_delivery_preferences(connection, self_id)
            due = [heartbeat_at]
            if await rows(connection, "SELECT 1 FROM digest_runs WHERE self_id=? AND kind='manual' AND status='queued' LIMIT 1", (self_id,)):
                return now
            if prefs.enabled:
                pending = await rows(connection, "SELECT MIN(scheduled_for) AS due FROM inbox_deliveries WHERE self_id=? AND status IN ('queued','deferred')", (self_id,))
                if pending[0]['due'] is not None:
                    due.append(pending[0]['due'])
                if prefs.digest_enabled:
                    scheduled = next_digest(prefs, now, self.config.timezone)
                    if scheduled is not None:
                        due.append(scheduled)
                    if await rows(connection, "SELECT 1 FROM digest_runs WHERE self_id=? AND kind!='manual' AND status IN ('queued','deferred') LIMIT 1", (self_id,)):
                        due.append(quiet_end(prefs, now, self.config.timezone) or now)
            return max(now, min(due))
