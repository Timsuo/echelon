from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["announcement", "schedule", "assignment", "exam", "material", "administrative",
                   "action_request", "project", "discussion", "other"]
Priority = Literal["critical", "high", "normal", "low"]
Label = Literal["deadline", "schedule_change", "location_change", "attendance", "submission", "file",
                "requires_reply", "requires_preparation", "exam", "assignment", "administrative"]
Identifier = Annotated[int, Field(gt=0, le=2**63 - 1)]
Keyword = Annotated[str, Field(min_length=1, max_length=40)]
Keywords = Annotated[list[Keyword], Field(max_length=30)]
Senders = Annotated[list[Identifier], Field(max_length=30)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class TriageItem(StrictModel):
    source_message_ids: list[Identifier] = Field(min_length=1, max_length=200)
    merge_into_item_id: Identifier | None
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    category: Category
    priority: Priority
    labels: list[Label] = Field(max_length=11)
    action_required: bool
    action_text: str | None = Field(max_length=500)
    deadline_text: str | None = Field(max_length=200)
    deadline_at: str | None = Field(max_length=64)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def consistency(self):
        if self.action_required != bool(self.action_text and self.action_text.strip()):
            raise ValueError("Action text must match action_required")
        if not self.action_required and self.action_text is not None:
            raise ValueError("Inactive action text must be null")
        if len(set(self.source_message_ids)) != len(self.source_message_ids) or len(set(self.labels)) != len(self.labels):
            raise ValueError("Duplicate reference or label")
        if self.deadline_at is not None:
            value = datetime.fromisoformat(self.deadline_at)
            if value.tzinfo is None or value.utcoffset() is None or not self.deadline_text:
                raise ValueError("Deadline requires explicit timezone and source text")
            if not 1970 <= value.year <= 2100:
                raise ValueError("Deadline outside supported range")
        return self


class TriageResult(StrictModel):
    items: list[TriageItem] = Field(max_length=10)
    ignored_message_ids: list[Identifier] = Field(max_length=200)

    def validate_references(self, messages: set[int], merge_ids: set[int]) -> None:
        used = {identifier for item in self.items for identifier in item.source_message_ids}
        ignored = set(self.ignored_message_ids)
        if used & ignored or used | ignored != messages or len(ignored) != len(self.ignored_message_ids):
            raise ValueError("Unaccounted, invented or conflicting source IDs")
        merges = [item.merge_into_item_id for item in self.items if item.merge_into_item_id is not None]
        if not set(merges) <= merge_ids or len(set(merges)) != len(merges):
            raise ValueError("Invalid or repeated merge target")


class TriagePreferences(StrictModel):
    important_keywords: Keywords = Field(default_factory=list)
    low_priority_keywords: Keywords = Field(default_factory=list)
    important_senders: Senders = Field(default_factory=list)
    category_priority_preferences: dict[Category, Priority] = Field(default_factory=dict, max_length=10)

    @field_validator("important_keywords", "low_priority_keywords")
    @classmethod
    def valid_keywords(cls, values):
        if any(not value.strip() or any(ord(c) < 32 for c in value) for value in values):
            raise ValueError("Invalid keyword")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate keyword")
        return values


class PreferenceChanges(StrictModel):
    important_keywords_add: Keywords = Field(default_factory=list)
    important_keywords_remove: Keywords = Field(default_factory=list)
    low_priority_keywords_add: Keywords = Field(default_factory=list)
    low_priority_keywords_remove: Keywords = Field(default_factory=list)
    important_senders_add: Senders = Field(default_factory=list)
    important_senders_remove: Senders = Field(default_factory=list)
    category_priority_preferences: dict[Category, Priority | None] = Field(default_factory=dict, max_length=10)

    def apply(self, current: TriagePreferences) -> TriagePreferences:
        data = current.model_dump()
        for key in ("important_keywords", "low_priority_keywords", "important_senders"):
            add, remove = getattr(self, key + "_add"), getattr(self, key + "_remove")
            if set(add) & set(remove):
                raise ValueError("Conflicting preference changes")
            data[key] = list(dict.fromkeys([v for v in data[key] if v not in remove] + add))
        for category, priority in self.category_priority_preferences.items():
            if priority is None:
                data["category_priority_preferences"].pop(category, None)
            else:
                data["category_priority_preferences"][category] = priority
        return TriagePreferences.model_validate(data)

    @model_validator(mode="after")
    def not_empty(self):
        if not any(self.model_dump().values()):
            raise ValueError("Empty preference diff")
        return self


class PreferenceIntent(StrictModel):
    action: Literal["update_triage_preferences"]
    changes: PreferenceChanges
    reason: str = Field(min_length=1, max_length=300)
