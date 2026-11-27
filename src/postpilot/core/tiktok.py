from dataclasses import dataclass
from typing import Literal

type TikTokMode = Literal["draft", "direct"]


@dataclass(frozen=True)
class TikTokChoice:
    privacy_level: str
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
