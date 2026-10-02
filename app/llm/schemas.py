import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Items = Annotated[list[ShortText], Field(max_length=30)]


class LegacyTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    summary: ShortText
    participants: Annotated[list[str], Field(max_length=100)]


class LegacySummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    topics: Annotated[list[LegacyTopic], Field(max_length=30)]
    decisions: Items
    todos: Items
    important_events: Items
    uncertainties: Items
    notable_message_ids: Annotated[list[str], Field(max_length=100)]

    @classmethod
    def empty(cls):
        return cls(topics=[], decisions=[], todos=[], important_events=[],
                   uncertainties=[], notable_message_ids=[])


TopicCategory = Literal['announcement', 'schedule', 'schedule_change', 'location_change', 'assignment',
                        'deadline', 'exam', 'material', 'administrative', 'discussion', 'project', 'action_request', 'other']


class Topic(LegacyTopic):
    start_message_id: str = Field(min_length=1, max_length=100)
    end_message_id: str = Field(min_length=1, max_length=100)
    notable_message_ids: list[str] = Field(default_factory=list, max_length=100)
    continuation: bool = False
    importance: Literal['high', 'normal', 'low'] = 'normal'
    category: TopicCategory = 'discussion'


class SummaryData(LegacySummaryData):
    schema_version: Literal[2] = 2
    topics: Annotated[list[Topic], Field(max_length=30)]


class TimedTopic(Topic):
    topic_start: int
    topic_end: int
    duration_seconds: int = Field(ge=0)


class StoredSummaryData(SummaryData):
    topics: Annotated[list[TimedTopic], Field(max_length=30)]


class CompactTopic(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    summary: ShortText
    importance: Literal['high', 'normal', 'low'] = 'normal'
    category: TopicCategory = 'discussion'
    topic_start: int | None = None
    topic_end: int | None = None
    continuation: bool = False


class CompactSummaryData(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    headline: str = Field(min_length=1, max_length=300)
    key_topics: list[CompactTopic] = Field(max_length=10)
    decisions: Items
    todos: Items
    important_events: Items
    uncertainties: Items
    omitted_topic_count: int = Field(ge=0, le=30)
    coverage_warning_present: bool


def add_topic_times(data: SummaryData, messages: list[dict]) -> StoredSummaryData:
    by_id = {row['message_id']: row['event_time'] for row in messages}
    if not set(data.notable_message_ids) <= set(by_id):
        raise ValueError('Unknown summary source ID')
    topics = []
    for topic in data.topics:
        references = {topic.start_message_id, topic.end_message_id, *topic.notable_message_ids}
        if not references <= set(by_id):
            raise ValueError('Unknown topic source ID')
        start, end = by_id[topic.start_message_id], by_id[topic.end_message_id]
        if start > end:
            raise ValueError('Reversed topic source times')
        topics.append(topic.model_dump() | dict(topic_start=start, topic_end=end, duration_seconds=end-start))
    return StoredSummaryData.model_validate(data.model_dump() | {'topics': topics})


def load_summary(raw: str, version: int | None = None):
    data = json.loads(raw)
    version = data.get('schema_version', version or 1)
    if version == 1:
        data.pop('schema_version', None)
        return LegacySummaryData.model_validate(data)
    if version == 2:
        return StoredSummaryData.model_validate(data)
    raise ValueError('Unknown summary version')
