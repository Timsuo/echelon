from app.operations.diagnostics import render_doctor, render_outbox


class OperationalCommands:
    def __init__(self, repository, actions, config):
        self.repository, self.actions, self.config = repository, actions, config

    async def account(self, event):
        if await self.repository.state('onebot_self_id') != str(event.self_id):
            raise ValueError('诊断账号不匹配')

    async def doctor(self, event, argument):
        await self.account(event)
        if argument:
            raise ValueError('用法：/doctor（只读诊断）')
        text = await render_doctor(self.repository, self.actions, self.config)
        await self.repository.notify(text, event.self_id)

    async def outbox(self, event, argument):
        await self.account(event)
        if argument not in {'', 'failed'}:
            raise ValueError('用法：/outbox [failed]')
        text = await render_outbox(self.repository, event.self_id, self.config.timezone, argument == 'failed')
        await self.repository.notify(text, event.self_id)
