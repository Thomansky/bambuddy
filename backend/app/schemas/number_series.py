from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class NumberSeriesResponse(BaseModel):
    """One running counter as the settings card renders it."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    enabled: bool
    prefix: str
    suffix: str
    next_value: int
    padding: int
    updated_at: datetime | None = None
    # What the next create would receive, rendered by the same helper the
    # allocator uses. Sent rather than left to the client so the card's preview
    # and the stored number can never disagree.
    preview: str


class NumberSeriesUpdate(BaseModel):
    """Partial update of a series. Omitted fields are left alone."""

    enabled: bool | None = None
    prefix: str | None = Field(default=None, max_length=16)
    suffix: str | None = Field(default=None, max_length=16)
    # Lowering this is allowed — it is the "start number" — but it can collide
    # with numbers already handed out, which the UI says in so many words.
    next_value: int | None = Field(default=None, ge=1)
    padding: int | None = Field(default=None, ge=0, le=16)
