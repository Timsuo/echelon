from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
Items = Annotated[list[ShortText], Field(max_length=30)]


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    summary: ShortText
    participants: Annotated[list[str], Field(max_length=100)]


class SummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    topics: Annotated[list[Topic], Field(max_length=30)]
    decisions: Items
    todos: Items
    important_events: Items
    uncertainties: Items
    notable_message_ids: Annotated[list[str], Field(max_length=100)]

    @classmethod
    def empty(cls) -> "SummaryData":
        return cls(topics=[], decisions=[], todos=[], important_events=[],
                   uncertainties=[], notable_message_ids=[])
