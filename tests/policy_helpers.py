from app.policies.models import PROFILES, GroupPolicy
from app.storage.policy_repository import save_policy


async def set_policy(repository, mode="inbox", self_id=88, group_id=123, **overrides):
    await repository.authorizations.activate(self_id, group_id)
    policy = GroupPolicy(self_id=self_id, group_id=group_id, mode=mode, **(PROFILES[mode] | overrides))
    async with repository.db.transaction() as connection:
        await save_policy(connection, policy)
    return policy
