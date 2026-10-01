from pydantic import BaseModel, ConfigDict, Field


class GroupFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    self_id: int = Field(gt=0)
    group_id: int = Field(gt=0)
    message_id: str
    file_id: str | None = Field(default=None, min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=1024)
    file_size: int | None = Field(default=None, ge=0, le=2**63 - 1)
    busid: int = Field(default=0, ge=0, le=2**31 - 1)
    source_type: str = "message"
    source_key: str
    # Ephemeral adapter cache only; never stored as a permanent download locator.
    url: str | None = Field(default=None, max_length=8192)
