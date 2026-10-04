import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.storage.outbox_repository import PREDECESSOR

GROWTH_TABLES = ('private_outbox', 'command_receipts', 'configuration_requests', 'configuration_proposals',
                 'triage_jobs', 'history_sync_jobs', 'digest_runs', 'delivery_revision_state')


def age(timestamp, now):
    if timestamp is None:
        return '—'
    seconds = max(0, int(now-float(timestamp)))
    return f'{seconds//3600}h {(seconds%3600)//60}m' if seconds >= 3600 else f'{seconds//60}m {seconds%60}s'


def error_class(value):
    return value if value and value.isascii() and value.isidentifier() and len(value) <= 80 else 'LegacyError'


async def outbox_stats(repository, self_id, now=None):
    now = time.time() if now is None else now
    pending = "o.sent_at IS NULL AND o.cancelled_at IS NULL AND o.dead_letter_at IS NULL"
    found = await repository.query(f"""SELECT
        COALESCE(SUM({pending} AND o.next_attempt<=? AND {PREDECESSOR}),0) AS ready,
        COALESCE(SUM({pending} AND o.attempts>0 AND o.next_attempt>?),0) AS retrying,
        COALESCE(SUM({pending} AND NOT(o.attempts>0 AND o.next_attempt>?)
          AND NOT(o.next_attempt<=? AND {PREDECESSOR})),0) AS deferred,
        COUNT(DISTINCT CASE WHEN o.dead_letter_at IS NOT NULL THEN COALESCE(o.producer_kind,'legacy')||':'||COALESCE(o.producer_key,CAST(o.id AS TEXT)) END) AS dead,
        MIN(CASE WHEN {pending} THEN o.created_at END) AS oldest,
        SUM(o.self_id IS NULL) AS legacy
        FROM private_outbox o WHERE o.self_id=? OR o.self_id IS NULL""", (now, now, now, now, self_id))
    return found[0]


async def failed_producers(repository, self_id):
    # No text, snapshot or provider body is selected.
    return await repository.query("""SELECT id,producer_kind,producer_key,error,attempts,dead_letter_at FROM (
        SELECT id,producer_kind,producer_key,error,attempts,dead_letter_at,
        ROW_NUMBER() OVER (PARTITION BY self_id,producer_kind,COALESCE(producer_key,CAST(id AS TEXT))
           ORDER BY CASE WHEN error='producer_chunk_failed' THEN 1 ELSE 0 END,id) AS n
        FROM private_outbox WHERE (self_id=? OR self_id IS NULL) AND dead_letter_at IS NOT NULL)
        WHERE n=1 ORDER BY dead_letter_at DESC,id DESC LIMIT 10""", (self_id,))


async def render_outbox(repository, self_id, timezone, failed=False):
    now = time.time()
    lines = ['【📤 Echelon Outbox】']
    if not failed:
        stats = await outbox_stats(repository, self_id, now)
        lines.extend([f'Ready：{stats["ready"]}', f'Retrying：{stats["retrying"]}', f'Deferred：{stats["deferred"]}',
                      f'Dead Letter：{stats["dead"]} producers', f'Oldest Pending：{age(stats["oldest"], now)}',
                      f'Legacy NULL account rows：{stats["legacy"] or 0}'])
    failures = await failed_producers(repository, self_id)
    lines.append('最近失败：')
    for row in failures if failed else failures[:1]:
        kind = row['producer_kind'] if row['producer_kind'] in {'delivery', 'digest', 'summary', 'interactive', 'file', 'history'} else 'legacy'
        key = row['producer_key'] or '—'
        if key != '—' and (not key.isascii() or not key.isalnum() or len(key) > 64):
            key = 'unknown'
        stamp = datetime.fromtimestamp(row['dead_letter_at'], ZoneInfo(timezone)).strftime('%m-%d %H:%M:%S')
        lines.append(f"#{row['id']} · {kind} · key={key} · {error_class(row['error'])} · attempts={row['attempts']} · {stamp}")
    if not failures:
        lines.append('无')
    if not failed:
        lines.append('/outbox failed')
    return '\n'.join(lines)


async def health_warning(repository, actions):
    sid = int(await repository.state('onebot_self_id') or 0)
    stale = await repository.query('SELECT 1 FROM worker_health WHERE expected=1 AND ?-COALESCE(last_success,started_at)>stale_seconds LIMIT 1', (time.time(),))
    dead = await repository.query('SELECT 1 FROM private_outbox WHERE (self_id=? OR self_id IS NULL) AND dead_letter_at IS NOT NULL LIMIT 1', (sid,))
    return not actions.connected or bool(stale or dead)


async def render_doctor(repository, actions, config):
    now = time.time()
    lines = ['【🩺 Echelon Doctor】', 'Service：✅ Running',
             'OneBot：' + ('✅ Connected' if actions.connected else '⚠️ Disconnected')]
    try:
        sid = int(await repository.state('onebot_self_id') or 0)
    except Exception:
        return '\n'.join(lines + ['Database：⚠️ unavailable'])
    # Every module is independent. Diagnostics never echo exception details.
    try:
        check = await repository.query('PRAGMA quick_check')
        healthy = len(check) == 1 and next(iter(check[0].values())) == 'ok'
        lines.append('Database：' + ('✅ quick_check' if healthy else '⚠️ quick_check failed'))
    except Exception as error:
        lines.append(f'Database：⚠️ unavailable ({type(error).__name__})')
    try:
        lines.append(f'Authorized Groups：{len(await repository.authorizations.list_active(sid))}')
    except Exception as error:
        lines.append(f'Authorized Groups：⚠️ unavailable ({type(error).__name__})')
    try:
        last = await repository.state(f'last_realtime_received_at:{sid}')
        gaps = await repository.query("SELECT count(*) AS n FROM collection_gaps WHERE self_id=? AND recovery_status!='likely_covered'", (sid,))
        lines.append(f'Collection：Last realtime {age(last, now)} ago · Unresolved gaps {gaps[0]["n"]}' + (' ⚠️' if gaps[0]['n'] else ''))
    except Exception as error:
        lines.append(f'Collection：⚠️ unavailable ({type(error).__name__})')
    try:
        for title, table in (('Triage', 'triage_jobs'), ('History', 'history_sync_jobs'), ('Summary', 'summary_jobs')):
            try:
                groups = await repository.query(f'SELECT status,count(*) AS n,MIN(created_at) AS oldest FROM {table} WHERE self_id=? GROUP BY status', (sid,))
                states = {r['status']: r for r in groups}
                oldest = states.get('queued', {}).get('oldest')
                pending = 0
                if title == 'Triage':
                    messages = (await repository.query("SELECT count(*) AS n,MIN(created_at) AS oldest FROM triage_message_state WHERE self_id=? AND status='pending'", (sid,)))[0]
                    pending = messages['n']
                    oldest = min(v for v in (oldest, messages['oldest']) if v is not None) if oldest or messages['oldest'] else None
                recent = (await repository.query(f"SELECT count(*) AS n FROM {table} WHERE self_id=? AND status='failed' AND completed_at>=?", (sid, now-86400)))[0]['n']
                counts = ' · '.join(f'{name} {states.get(name, {}).get("n", 0)}' for name in ('queued', 'running', 'failed'))
                lines.append(f'{title}：Pending messages {pending} · {counts} · Failed 24h {recent}\nOldest queued：{age(oldest, now)}' + (' ⚠️' if oldest is not None and now-oldest > 1200 else ''))
            except Exception as error:
                lines.append(f"{title}：⚠️ unavailable ({type(error).__name__})")
    except Exception as error:
        lines.append(f'Jobs：⚠️ unavailable ({type(error).__name__})')
    try:
        delivery = (await repository.query("SELECT SUM(status='deferred' AND failed_at IS NULL) AS deferred,SUM(failed_at IS NOT NULL) AS failed,MIN(CASE WHEN status IN ('queued','deferred') AND failed_at IS NULL THEN created_at END) AS oldest FROM inbox_deliveries WHERE self_id=?", (sid,)))[0]
        digests = (await repository.query("SELECT SUM(failed_at IS NOT NULL) AS failed,MIN(CASE WHEN status IN ('queued','deferred') AND failed_at IS NULL THEN created_at END) AS oldest FROM digest_runs WHERE self_id=?", (sid,)))[0]
        settings = await repository.query('SELECT last_heartbeat FROM delivery_settings WHERE self_id=?', (sid,))
        oldest = [d['oldest'] for d in (delivery, digests) if d['oldest'] is not None]
        lines.append(f'Delivery：Heartbeat {age(settings[0]["last_heartbeat"] if settings else None, now)} ago · Deferred {delivery["deferred"] or 0} · Failed {(delivery["failed"] or 0)+(digests["failed"] or 0)}\nOldest queued：{age(min(oldest) if oldest else None, now)}')
    except Exception as error:
        lines.append(f'Delivery：⚠️ unavailable ({type(error).__name__})')
    try:
        outbox = await outbox_stats(repository, sid, now)
        lines.append(f'Outbox：Ready {outbox["ready"]} · Retrying {outbox["retrying"]} · Deferred {outbox["deferred"]} · Dead Letter {outbox["dead"]}\nOldest Pending：{age(outbox["oldest"], now)}')
    except Exception as error:
        lines.append(f'Outbox：⚠️ unavailable ({type(error).__name__})')
    try:
        health = await repository.state('deepseek_health')
        # Only known status labels, never arbitrary stored error text.
        lines.append(f'DeepSeek：{health if health in {"Healthy", "Failed"} else "Unknown"} · Last success {age(await repository.state("last_successful_api_call"), now)} ago')
    except Exception as error:
        lines.append(f'DeepSeek：⚠️ unavailable ({type(error).__name__})')
    try:
        attachments = await repository.query('SELECT download_status,count(*) AS n FROM attachments WHERE self_id=? GROUP BY download_status', (sid,))
        lines.append('Attachments：' + ' · '.join(f'{r["download_status"]} {r["n"]}' for r in attachments))
    except Exception as error:
        lines.append(f'Attachments：⚠️ unavailable ({type(error).__name__})')
    try:
        size = (await repository.query("SELECT COALESCE(SUM(file_size),0) AS bytes FROM attachments WHERE self_id=? AND download_status='downloaded'", (sid,)))[0]['bytes']
        db_bytes = repository.db.path.stat().st_size if repository.db.path.exists() else 0
        wal = repository.db.path.with_name(repository.db.path.name+'-wal')
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        lines.append(f'Storage：DB {db_bytes/1024**2:.2f} MiB · WAL {wal_bytes/1024**2:.2f} MiB · Attachments (recorded) {size/1024**2:.2f} MiB')
    except Exception as error:
        lines.append(f'Storage：⚠️ unavailable ({type(error).__name__})')
    try:
        lines.append('Workers：')
        for worker in await repository.query('SELECT * FROM worker_health ORDER BY name'):
            stale = now-(worker['last_success'] or worker['started_at']) > worker['stale_seconds']
            label = 'Stopped' if not worker['expected'] else ('⚠️ STALE' if stale else '✅ OK')
            lines.append(f'{worker["name"]}：{label} · {age(worker["last_success"], now)}')
    except Exception as error:
        lines.append(f'Workers：⚠️ unavailable ({type(error).__name__})')
    try:
        lines.append('Operational rows (current account)：')
        for table in GROWTH_TABLES:
            count = (await repository.query(f'SELECT count(*) AS n FROM {table} WHERE self_id=?', (sid,)))[0]['n']
            lines.append(f'{table}：{count}')
    except Exception as error:
        lines.append(f'Growth：⚠️ unavailable ({type(error).__name__})')
    lines.append('只读诊断；未执行修复或数据清理。/outbox failed 查看发送失败。')
    return '\n'.join(lines)
