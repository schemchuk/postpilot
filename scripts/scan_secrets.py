"""Scan repository text for common secret formats without echoing values."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "Anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "Telegram bot token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{35}(?![\w-])"),
    "Secret assignment": re.compile(
        r"\b[A-Z0-9_]*(?:KEY|TOKEN|SECRET)\s*=\s*['\"]?"
        r"(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/_-]{32,}={0,2})(?![\w+/=-])"
    ),
}
TEMPLATE_PLACEHOLDER = re.compile(
    r"""^\s*[A-Z0-9_]+\s*=\s*['"]?(?:<[^<>]+>|your[-_][\w-]+|"""
    r"""replace[-_]me|change[-_]me|placeholder)['"]?\s*(?:#.*)?$""",
    re.IGNORECASE,
)


def repository_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(
        {
            Path(name)
            for name in result.stdout.decode(errors="surrogateescape").split("\0")
            if name
        }
    )


def scan(path: Path) -> bool:
    if path.name != ".env.example" and (
        path.name == ".env" or path.name.startswith(".env.")
    ):
        return False
    if path.is_symlink() or not path.is_file():
        return False

    data = path.read_bytes()
    if b"\0" in data:
        return False

    found = False
    for number, line in enumerate(
        data.decode("utf-8", errors="replace").splitlines(), 1
    ):
        if path.name == ".env.example" and TEMPLATE_PLACEHOLDER.fullmatch(line):
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                print(f"{path}:{number}: possible {label}", file=sys.stderr)
                found = True
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Files to scan")
    args = parser.parse_args()

    found = False
    try:
        for path in args.paths or repository_paths():
            if scan(path):
                found = True
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Secret scan failed: {error}", file=sys.stderr)
        return 2
    return int(found)


if __name__ == "__main__":
    raise SystemExit(main())
