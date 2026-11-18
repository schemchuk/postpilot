import subprocess
import sys
from pathlib import Path

SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "scan_secrets.py"


def test_scanner_flags_fake_key(tmp_path: Path) -> None:
    fake_key = "ghp_" + "a" * 36
    source = tmp_path / "leak.txt"
    source.write_text(f"{fake_key}\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCANNER), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"{source}:1: possible GitHub token" in result.stderr
    assert fake_key not in result.stdout + result.stderr
