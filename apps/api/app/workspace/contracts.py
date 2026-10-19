from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Piece(Strict):
    location: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=60000)


class ConfirmRequest(Strict):
    revision: int = Field(ge=1)
    pieces: list[Piece] = Field(min_length=1, max_length=200)


class RevisionRequest(Strict):
    revision: int = Field(ge=1)


class SelectionRequest(Strict):
    document_ids: list[str] = Field(default_factory=list, max_length=5)


class PrivateSearch(Strict):
    query: str = Field(min_length=1, max_length=400)
    top_k: int = Field(default=3, ge=1, le=3)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("query cannot be blank")
        return value.strip()


class FeedbackRequest(Strict):
    run_id: str = Field(min_length=1, max_length=80)
    category: Literal["helpful", "off_topic", "irrelevant_source", "missing_document", "tool_failure", "other"]
    comment: str = Field(default="", max_length=300)
