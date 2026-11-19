from dataclasses import dataclass
from datetime import datetime

from postpilot.video.heygen import HeyGenClient
from postpilot.video.store import VideoStore


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str


def check_budget(
    store: VideoStore, heygen: HeyGenClient, now: datetime, cap: int, min_balance: float
) -> BudgetDecision:
    if store.count_this_month(now) >= cap:
        return BudgetDecision(False, "monthly video cap reached")
    if heygen.wallet_balance() < min_balance:
        return BudgetDecision(False, "HeyGen wallet balance below minimum")
    return BudgetDecision(True, "within budget")
