import logging
import time
from datetime import datetime

from app.config import AppConfig
from app.storage.authorization_repository import is_active
from app.storage.history_repository import rows
from app.storage.policy_repository import read_policy
from app.storage.repository import Repository
from app.triage.deadlines import validate_deadlines
from app.triage.models import TriagePreferences, TriageResult

logger = logging.getLogger(__name__)


class TriageRepository:
    def __init__(self, repository: Repository, config: AppConfig) -> None:
        self.repository, self.db, self.config = repository, repository.db, config

    async def recover(self) -> None:
        await self.repository.query("UPDATE triage_jobs SET status='queued' WHERE status='running'")

    async def claim(self, now: float | None = None) -> dict | None:
        if not self.config.triage.enabled:
            return None
        now = time.time() if now is None else now
        config = self.config.triage
        async with self.db.transaction() as connection:
            bound = await rows(connection, "SELECT value FROM runtime_state WHERE key='onebot_self_id'")
            if not bound:
                return None
            self_id = int(bound[0]['value'])
            # One transaction allocates ownership and claims work. No parallel candidates own a message.
            pending_groups = await rows(connection, "SELECT group_id,MIN(created_at) AS first_at,MAX(created_at) AS last_at,MIN(fast_track_at) AS fast_at "
                "FROM triage_message_state WHERE self_id=? AND status='pending' GROUP BY group_id ORDER BY first_at", (self_id,))
            for group in pending_groups:
                group_id = group['group_id']
                policy = await read_policy(connection, self_id, group_id)
                if not await is_active(connection, self_id, group_id) or policy.mode not in {'inbox', 'priority'} or not policy.inbox_enabled:
                    # Policy-paused pending messages remain durable, not silently discarded.
                    continue
                available = await rows(connection, "SELECT m.* FROM triage_message_state s JOIN messages m ON m.id=s.message_id "
                    "WHERE s.self_id=? AND s.group_id=? AND s.status='pending' ORDER BY s.message_id LIMIT ?",
                    (self_id, group_id, config.max_messages_per_candidate + 1))
                chosen, chars, times = [], 0, []
                for message in available:
                    cost = len(message['normalized_text']) + len(message['nickname']) + 250
                    proposed = times + [message['event_time']]
                    if chosen and (len(chosen) >= config.max_messages_per_candidate or
                        chars + cost > config.max_input_chars // 2 or max(proposed) - min(proposed) > config.max_span_seconds):
                        break
                    chosen.append(message)
                    chars += cost
                    times = proposed
                due = min(group['last_at'] + getattr(config, policy.mode + '_debounce_seconds'),
                          group['first_at'] + getattr(config, policy.mode + '_max_wait_seconds'))
                if group['fast_at'] is not None:
                    from app.delivery.preferences import read_delivery_preferences
                    delivery_prefs = await read_delivery_preferences(connection, self_id)
                    if delivery_prefs.enabled and delivery_prefs.fast_track_enabled:
                        due = min(due, max(group['fast_at'], group['last_at'] + delivery_prefs.fast_track_debounce_seconds))
                if now < due and len(chosen) == len(available) and len(chosen) < config.max_messages_per_candidate:
                    continue
                sources = {m['ingest_source'] == 'realtime' for m in chosen}
                source = 'mixed' if len(sources) > 1 else ('realtime' if True in sources else 'history_recovery')
                job = (await rows(connection, "INSERT INTO triage_jobs(self_id,group_id,window_start,window_end,created_at,not_before,source_kind) "
                    "VALUES (?,?,?,?,?,?,?) RETURNING *", (self_id, group_id, min(times), max(times) + 1, now, now, source)))[0]
                for message in chosen:
                    await connection.execute("INSERT INTO triage_job_messages VALUES (?,?,?,?)", (job['id'], message['id'], self_id, group_id))
                    await connection.execute("UPDATE triage_message_state SET status='queued',triage_job_id=? WHERE message_id=? AND status='pending'",
                                             (job['id'], message['id']))
                logger.info("Triage candidate created id=%s messages=%s", job['id'], len(chosen))
            claimed = await rows(connection, "UPDATE triage_jobs SET status='running',started_at=?,attempts=attempts+1,"
                "retry_count=attempts WHERE id=(SELECT id FROM triage_jobs WHERE self_id=? AND status='queued' AND not_before<=? "
                "AND EXISTS (SELECT 1 FROM group_authorizations a WHERE a.self_id=triage_jobs.self_id AND a.group_id=triage_jobs.group_id AND a.active=1) "
                "ORDER BY id LIMIT 1) AND status='queued' RETURNING *", (now, self_id, now))
            return claimed[0] if claimed else None

    async def context(self, job: dict) -> tuple[list[dict], list[dict], list[dict]]:
        async with self.db.transaction() as connection:
            messages = await rows(connection, "SELECT m.* FROM messages m JOIN triage_job_messages l ON l.message_id=m.id "
                "AND l.self_id=m.self_id AND l.group_id=m.group_id WHERE l.job_id=? AND l.self_id=? AND l.group_id=? ORDER BY m.event_time,m.id",
                (job['id'], job['self_id'], job['group_id']))
            attachments = await rows(connection, "SELECT a.message_id,a.filename,a.file_size,a.download_status FROM attachments a "
                "JOIN triage_job_messages l ON l.message_id=a.message_id AND l.self_id=a.self_id AND l.group_id=a.group_id "
                "WHERE l.job_id=? AND l.self_id=?", (job['id'], job['self_id']))
            candidates = await rows(connection, "SELECT i.id,i.title,i.summary,i.category,i.priority,i.deadline_at,i.deadline_text,i.updated_at,i.revision "
                "FROM inbox_items i WHERE i.self_id=? AND i.source_group_id=? AND i.status!='archived' "
                "AND (i.updated_at>=? OR EXISTS (SELECT 1 FROM inbox_item_messages l JOIN triage_job_messages t ON t.message_id=l.message_id "
                "AND t.self_id=l.self_id WHERE l.inbox_item_id=i.id AND t.job_id=?)) "
                "ORDER BY EXISTS (SELECT 1 FROM inbox_item_messages l JOIN triage_job_messages t ON t.message_id=l.message_id "
                "AND t.self_id=l.self_id WHERE l.inbox_item_id=i.id AND t.job_id=?) DESC,i.updated_at DESC LIMIT 10",
                (job['self_id'], job['group_id'], time.time() - 7 * 86400, job['id'], job['id']))
            return messages, attachments, candidates

    async def apply(self, job: dict, result: TriageResult, candidates: list[dict], coverage_warning: bool, model: str) -> None:
        async with self.db.transaction() as connection:
            active = await rows(connection, "SELECT * FROM triage_jobs WHERE id=? AND self_id=? AND status='running' AND attempts=?",
                                (job['id'], job['self_id'], job['attempts']))
            if not active:
                raise ValueError("Triage job is not running")
            bound = await rows(connection, "SELECT value FROM runtime_state WHERE key='onebot_self_id'")
            policy = await read_policy(connection, job['self_id'], job['group_id'])
            if not bound or bound[0]['value'] != str(job['self_id']) or not await is_active(connection, job['self_id'], job['group_id']) or policy.mode not in {'inbox', 'priority'} or not policy.inbox_enabled:
                raise PermissionError("Triage account or policy changed")
            messages = await rows(connection, "SELECT m.* FROM messages m JOIN triage_job_messages l ON l.message_id=m.id "
                "AND l.self_id=m.self_id AND l.group_id=m.group_id WHERE l.job_id=? AND l.self_id=?", (job['id'], job['self_id']))
            by_id = {m['id']: m for m in messages}
            permitted = {c['id']: c for c in candidates}
            result.validate_references(set(by_id), set(permitted))
            validate_deadlines(result, messages, self.config.timezone)
            # Recheck gaps under the apply lock: a disconnect may occur while inference is running.
            new_gaps = await rows(connection, "SELECT g.id FROM collection_gaps g LEFT JOIN history_sync_jobs j "
                "ON j.gap_id=g.id AND j.self_id=g.self_id AND j.group_id=? WHERE g.self_id=? AND g.started_at<? "
                "AND COALESCE(g.ended_at,?)>? AND (j.coverage IS NULL OR j.coverage!='likely_covered' OR j.status!='completed')",
                (job['group_id'], job['self_id'], job['window_end'], job['window_end'], job['window_start']))
            coverage_warning = coverage_warning or bool(new_gaps)
            used_targets = set()
            for item in result.items:
                ids = item.source_message_ids
                placeholders = ','.join('?' for _ in ids)
                linked = await rows(connection, f"SELECT DISTINCT i.* FROM inbox_items i JOIN inbox_item_messages l ON l.inbox_item_id=i.id "
                    f"AND l.self_id=i.self_id WHERE l.message_id IN ({placeholders}) AND i.self_id=? AND i.source_group_id=? "
                    "AND i.origin_key LIKE 'attachment:%' AND i.triaged_at IS NULL AND i.status!='archived' ORDER BY i.id",
                    (*ids, job['self_id'], job['group_id']))
                target = item.merge_into_item_id
                if target is None and linked:
                    # Deterministic reuse prevents the Phase 2 file placeholder + LLM duplicate pattern.
                    target = linked[0]['id']
                if target in used_targets:
                    raise ValueError("Conflicting updates to same Inbox item")
                current = None
                if target is not None:
                    existing = await rows(connection, "SELECT * FROM inbox_items WHERE id=? AND self_id=? AND source_group_id=? AND status!='archived'",
                                          (target, job['self_id'], job['group_id']))
                    if not existing or (item.merge_into_item_id is not None and target not in permitted):
                        raise ValueError("Merge target no longer eligible")
                    current = existing[0]
                    if target in permitted and current['revision'] != permitted[target]['revision']:
                        raise ValueError("Merge target revised during inference")
                    used_targets.add(target)
                deadline = int(datetime.fromisoformat(item.deadline_at).timestamp()) if item.deadline_at else None
                # Preferences already participate in the model context; retain original model judgment explicitly.
                values = dict(title=item.title, summary=item.summary, category=item.category, priority=item.priority,
                    model_priority=item.priority, action_required=int(item.action_required), action_text=item.action_text,
                    deadline_text=item.deadline_text, deadline_at=deadline, reason=item.reason, confidence=item.confidence)
                now = time.time()
                if current is None:
                    first = min((by_id[i] for i in ids), key=lambda m: (m['event_time'], m['id']))
                    target = (await rows(connection, "INSERT INTO inbox_items(self_id,title,source_group_id,source_sender_id,event_time,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?) RETURNING id", (job['self_id'], item.title, job['group_id'], first['user_id'], first['event_time'], now, now)))[0]['id']
                    revision = 1
                else:
                    old_labels = {r['label'] for r in await rows(connection, "SELECT label FROM inbox_item_labels WHERE inbox_item_id=? AND self_id=?", (target, job['self_id']))}
                    material = any(current[key] != values[key] for key in
                        ('title', 'summary', 'category', 'priority', 'action_required', 'action_text', 'deadline_text', 'deadline_at')) or old_labels != set(item.labels)
                    revision = current['revision'] + int(material)
                if current is None or revision > current['revision']:
                    await connection.execute("INSERT INTO inbox_revision_events(self_id,inbox_item_id,revision,triage_job_id,group_id,created_at) VALUES (?,?,?,?,?,?)",
                        (job['self_id'], target, revision, job['id'], job['group_id'], now))
                coverage = 'warning' if coverage_warning or (current and current['coverage_status'] == 'warning') else 'no_known_gap'
                await connection.execute(f"UPDATE inbox_items SET {','.join(key+'=?' for key in values)},revision=?,triaged_at=?,triage_model=?,coverage_status=?,updated_at=? WHERE id=? AND self_id=?",
                    (*values.values(), revision, now, model, coverage, now, target, job['self_id']))
                await connection.execute("DELETE FROM inbox_item_labels WHERE inbox_item_id=? AND self_id=?", (target, job['self_id']))
                for label in item.labels:
                    await connection.execute("INSERT INTO inbox_item_labels VALUES (?,?,?)", (target, job['self_id'], label))
                for identifier in ids:
                    await connection.execute("INSERT INTO inbox_item_messages VALUES (?,?,?) ON CONFLICT DO NOTHING", (target, identifier, job['self_id']))
                await connection.execute(f"INSERT INTO inbox_item_attachments SELECT ?,id,self_id FROM attachments WHERE self_id=? AND group_id=? AND message_id IN ({placeholders}) ON CONFLICT DO NOTHING",
                                         (target, job['self_id'], job['group_id'], *ids))
                for placeholder in linked:
                    if placeholder['id'] == target:
                        continue
                    # Preserve provenance; only untouched file placeholders are consolidated, never arbitrary items.
                    for table, column in (('inbox_item_messages', 'message_id'), ('inbox_item_attachments', 'attachment_id')):
                        await connection.execute(f"INSERT INTO {table} SELECT ?,{column},self_id FROM {table} WHERE inbox_item_id=? AND self_id=? ON CONFLICT DO NOTHING",
                                                 (target, placeholder['id'], job['self_id']))
                    await connection.execute("UPDATE inbox_items SET status='archived',updated_at=? WHERE id=? AND self_id=?", (now, placeholder['id'], job['self_id']))
            await connection.execute("UPDATE triage_message_state SET status='processed' WHERE triage_job_id=? AND self_id=?", (job['id'], job['self_id']))
            for identifier in result.ignored_message_ids:
                await connection.execute("UPDATE triage_message_state SET status='ignored' WHERE message_id=? AND triage_job_id=? AND self_id=?", (identifier, job['id'], job['self_id']))
            await connection.execute("UPDATE triage_jobs SET status='completed',completed_at=?,error=NULL,result_json=? WHERE id=? AND self_id=?",
                                     (time.time(), result.model_dump_json(), job['id'], job['self_id']))
            logger.info("Triage applied job=%s items=%s", job['id'], len(result.items))

    async def fail(self, job: dict, error: str, retry: bool) -> None:
        retry = retry and job['attempts'] <= self.config.triage.retry_count
        async with self.db.transaction() as connection:
            changed = await rows(connection, "UPDATE triage_jobs SET status=?,error=?,not_before=?,completed_at=? "
                "WHERE id=? AND self_id=? AND status='running' AND attempts=? RETURNING id",
                ('queued' if retry else 'failed', error, time.time() + min(60, 2 ** min(job['attempts'], 6)),
                 None if retry else time.time(), job['id'], job['self_id'], job['attempts']))
            if changed and not retry:
                await connection.execute("UPDATE triage_message_state SET status='failed' WHERE triage_job_id=? AND self_id=?", (job['id'], job['self_id']))

    async def preferences(self, self_id: int) -> TriagePreferences:
        result = await self.repository.query("SELECT preferences_json FROM triage_preferences WHERE self_id=?", (self_id,))
        return TriagePreferences.model_validate_json(result[0]['preferences_json']) if result else TriagePreferences()
