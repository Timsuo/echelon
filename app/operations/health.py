import time


class WorkerHealth:
    def __init__(self, db):
        self.db = db
        self.last_write = {}

    async def start(self, thresholds):
        now = time.time()
        async with self.db.transaction() as connection:
            await connection.execute('UPDATE worker_health SET expected=0')
            for name, threshold in thresholds.items():
                await connection.execute('INSERT INTO worker_health VALUES (?,1,?,?,?) ON CONFLICT(name) DO UPDATE SET '
                    'expected=1,started_at=excluded.started_at,last_success=excluded.last_success,stale_seconds=excluded.stale_seconds',
                    (name, now, now, threshold))
        self.last_write = {name: time.monotonic() for name in thresholds}

    async def beat(self, name):
        # A real successful loop calls this; no timer can conceal a blocked worker.
        if name not in self.last_write or time.monotonic()-self.last_write[name] < 30:
            return
        async with self.db.transaction() as connection:
            await connection.execute('UPDATE worker_health SET last_success=? WHERE name=? AND expected=1', (time.time(), name))
        self.last_write[name] = time.monotonic()

    async def stop(self):
        async with self.db.transaction() as connection:
            await connection.execute('UPDATE worker_health SET expected=0')


async def beat(db, name):
    await db.health.beat(name)
