import time

from pydantic import ValidationError

from app.commands.policies import PolicyCommands
from app.delivery.preferences import DeliveryPreferenceRepository, parse_local
from app.delivery.renderer import stamp
from app.delivery.repository import DeliveryRepository
from app.delivery.schedule import next_digest


class DeliveryCommands(PolicyCommands):
    def __init__(self, repository, config, admin_qq):
        super().__init__(repository, None, admin_qq)
        self.config = config
        self.preferences = DeliveryPreferenceRepository(self.policies)
        self.deliveries = DeliveryRepository(repository, config)

    async def delivery(self, event, argument):
        await self.account(event)
        if argument:
            raise ValueError('用法：/delivery')
        prefs = await self.preferences.get(event.self_id)
        deferred = await self.repository.query("SELECT count(*) AS n FROM inbox_deliveries WHERE self_id=? AND status='deferred'", (event.self_id,))
        settings = await self.repository.query('SELECT last_heartbeat FROM delivery_settings WHERE self_id=?', (event.self_id,))
        last = settings[0]['last_heartbeat'] if settings else None
        healthy = last is not None and time.time()-last <= prefs.heartbeat_seconds*2+5
        lines = ['【Echelon Delivery】', f'Heartbeat：{"ON" if prefs.enabled else "OFF"} · {prefs.heartbeat_seconds}s · {"Healthy" if healthy else "Waiting"}',
                 'Urgent：' + (', '.join(prefs.urgent_priorities).upper() if prefs.urgent_enabled else 'OFF'),
                 f'Quiet Hours：{prefs.quiet_start}–{prefs.quiet_end}' if prefs.quiet_hours_enabled else 'Quiet Hours：OFF',
                 f'Critical Override：{prefs.critical_break_quiet_hours}', f'High Override：{prefs.high_break_quiet_hours}',
                 'Digest：' + (', '.join(prefs.digest_times) if prefs.digest_enabled else 'OFF'),
                 'Next Digest：' + stamp(next_digest(prefs, time.time(), self.config.timezone), self.config.timezone),
                 f'Recovery Alert：{prefs.recovery_alert_max_age_hours}h · {prefs.recovery_alert_enabled}',
                 f'Deferred：{deferred[0]["n"]}', '/notify <自然语言>', 'Heartbeat 只后台检查，不会每分钟发消息。']
        await self.repository.notify('\n'.join(lines), event.self_id)

    async def notify(self, event, argument):
        await self.account(event)
        if not argument or len(argument) > 2000:
            raise ValueError('用法：/notify <自然语言>（最多2000字）')
        try:
            intent = parse_local(argument)
            if intent:
                await self.preferences.propose(dict(self_id=event.self_id, admin_qq=self.admin_qq), intent, message_id=event.message_id)
            else:
                await self.preferences.queue(event, argument)
        except ValidationError:
            raise ValueError('投递配置字段不合法，请检查 HH:MM、时间数量与范围。') from None

    async def digest(self, event, argument):
        await self.account(event)
        if argument != 'now':
            raise ValueError('用法：/digest now')
        await self.deliveries.manual(event.self_id, event.message_id)
