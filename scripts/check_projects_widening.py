"""Check two project configs for policy widenings."""

import argparse
import sys
from pathlib import Path

from postpilot.projects import Projects, ProjectsConfigError, widenings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_file", type=Path, metavar="BASE_FILE")
    parser.add_argument("head_file", type=Path, metavar="HEAD_FILE")
    args = parser.parse_args()

    try:
        try:
            base = Projects.load(args.base_file)
        except ProjectsConfigError as error:
            if not isinstance(error.__cause__, FileNotFoundError):
                raise
            base = Projects()
        head = Projects.load(args.head_file)
    except ProjectsConfigError as error:
        print(f"Invalid projects config: {error}", file=sys.stderr)
        return 2

    changes = widenings(base, head)
    for change in changes:
        print(change)
    return int(bool(changes))


if __name__ == "__main__":
    raise SystemExit(main())
