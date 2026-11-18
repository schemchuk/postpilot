from importlib import import_module

import postpilot
from postpilot.cli import main


def test_import_and_cli() -> None:
    for name in (
        "core",
        "sources",
        "ai",
        "video",
        "channels",
        "tokens",
        "notify",
        "store",
    ):
        import_module(f"postpilot.{name}")

    assert postpilot.__version__
    assert main([]) == 0
