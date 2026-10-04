from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

Mode = Literal["summary_only", "inbox", "priority", "ignore"]
PROFILES = {
    "summary_only": dict(summary_enabled=True, inbox_enabled=False,
                         attachment_download_enabled=False, priority_watch_enabled=False),
    "inbox": dict(summary_enabled=True, inbox_enabled=True,
                  attachment_download_enabled=True, priority_watch_enabled=False),
    "priority": dict(summary_enabled=True, inbox_enabled=True,
                     attachment_download_enabled=True, priority_watch_enabled=True),
    "ignore": dict(summary_enabled=False, inbox_enabled=False,
                   attachment_download_enabled=False, priority_watch_enabled=False),
}


class PolicyChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    alias: str | None = Field(default=None, min_length=1, max_length=64)
    mode: Mode | None = None
    summary_enabled: bool | None = None
    inbox_enabled: bool | None = None
    attachment_download_enabled: bool | None = None
    priority_watch_enabled: bool | None = None

    @field_validator("alias")
    @classmethod
    def valid_alias(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("Invalid alias")
        return value

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Changes must contain explicit non-null values")
        return self

    def expanded(self) -> dict:
        changes = self.model_dump(exclude_unset=True)
        return (PROFILES[self.mode] | changes) if self.mode else changes


class ConfigIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["update_group_policy"]
    changes: PolicyChanges
    reason: str = Field(min_length=1, max_length=300)


class ConfigFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["clarify", "unsupported"]
    # Never relay this untrusted text; the application renders safe guidance.
    message: str = Field(min_length=1, max_length=300)


class ConfigParseResult(RootModel[Annotated[ConfigIntent | ConfigFeedback, Field(discriminator="action")]]):
    """Flat JSON wire format; existing update intents remain compatible."""


class GroupPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    self_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    alias: str | None = None
    mode: Mode = "summary_only"
    summary_enabled: bool = True
    inbox_enabled: bool = False
    attachment_download_enabled: bool = False
    priority_watch_enabled: bool = False


POLICY_FIELDS = tuple(PolicyChanges.model_fields)
