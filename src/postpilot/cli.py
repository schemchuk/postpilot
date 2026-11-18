"""Command-line entry point."""

from postpilot import __version__

PLANNED_COMMANDS = (
    "draft-weekly",
    "poll-telegram",
    "publish-due",
    "snapshot-traffic",
    "check-tokens",
)


def main() -> int:
    print(f"postpilot {__version__}")
    print("Planned commands:")
    for command in PLANNED_COMMANDS:
        print(f"  {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
