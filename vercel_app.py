"""Vercel entrypoint for the Telegram webhook receiver (ADR 021)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from postpilot.webhook import app

__all__ = ["app"]
