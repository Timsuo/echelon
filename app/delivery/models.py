import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator


class DeliveryPreferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool = True
    heartbeat_seconds: int = Field(default=60, ge=10, le=3600)
    urgent_enabled: bool = True
    urgent_priorities: list[Literal['critical', 'high']] = Field(default_factory=lambda: ['critical', 'high'], max_length=2)
    fast_track_enabled: bool = True
    fast_track_debounce_seconds: int = Field(default=30, ge=30, le=3600)
    digest_enabled: bool = True
    digest_times: list[str] = Field(default_factory=lambda: ['07:30', '12:20', '18:00', '22:30'])
    quiet_hours_enabled: bool = True
    quiet_start: str = '23:30'
    quiet_end: str = '07:00'
    critical_break_quiet_hours: bool = True
    high_break_quiet_hours: bool = False
    send_empty_digest: bool = False
    digest_include_read: bool = False
    digest_include_group_summaries: bool = False
    digest_catchup_minutes: int = Field(default=180, ge=1, le=1440)
    recovery_alert_enabled: bool = True
    recovery_alert_max_age_hours: int = Field(default=12, ge=1, le=168)

    @field_validator('quiet_start', 'quiet_end')
    @classmethod
    def clock(cls, value):
        if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValueError('时间必须为 HH:MM')
        return value

    @field_validator('digest_times')
    @classmethod
    def times(cls, value):
        unique = sorted({cls.clock(v) for v in value})
        if not 1 <= len(unique) <= 12:
            raise ValueError('每天需要1到12个收信时间')
        return unique

    @field_validator('urgent_priorities')
    @classmethod
    def priorities(cls, value):
        return sorted(set(value))

    @model_validator(mode='after')
    def quiet_range(self):
        if self.quiet_start == self.quiet_end:
            raise ValueError('静默开始和结束不能相同')
        return self


class ChangesBase(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('Changes require explicit non-null fields')
        return self


DeliveryChanges = create_model('DeliveryChanges', __base__=ChangesBase, **{
    name: (Annotated[field.annotation | None, *field.metadata] if field.metadata else field.annotation | None, None)
    for name, field in DeliveryPreferences.model_fields.items()
})


class DeliveryPreferenceIntent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['update_delivery_preferences']
    changes: DeliveryChanges
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode='after')
    def validate_clocks(self):
        for name in ('quiet_start', 'quiet_end'):
            if name in self.changes.model_fields_set:
                DeliveryPreferences.clock(getattr(self.changes, name))
        if 'digest_times' in self.changes.model_fields_set:
            self.changes.digest_times = DeliveryPreferences.times(self.changes.digest_times)
        return self
