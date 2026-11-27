from dataclasses import dataclass
from typing import Literal

type TikTokMode = Literal["draft", "direct"]
type TikTokToggle = Literal["comment", "duet", "stitch"]


@dataclass(frozen=True)
class TikTokSelection:
    privacy_level: str | None = None
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False


@dataclass(frozen=True)
class TikTokChoice:
    privacy_level: str
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
