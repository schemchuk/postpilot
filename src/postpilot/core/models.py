from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from postpilot.core.states import ItemState

type Source = Literal["github", "telegram"]
type Language = Literal["de", "ua"]


class PublicationStatus(StrEnum):
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Item:
    id: UUID
    source: Source
    state: ItemState
    created_at: datetime
    updated_at: datetime
    approved_by: int | None
    approved_at: datetime | None


@dataclass(frozen=True)
class Variant:
    item_id: UUID
    lang: Language
    text: str


@dataclass(frozen=True)
class Publication:
    item_id: UUID
    channel: str
    status: PublicationStatus
    remote_id: str | None
    error: str | None
    updated_at: datetime
