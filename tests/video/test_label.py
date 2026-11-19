import shutil
import subprocess
from pathlib import Path

import pytest

from postpilot.video.label import burn_ai_label


def test_drawtext_argv(monkeypatch):
    def run(argv, **kwargs):
        assert isinstance(argv, list)
        assert argv[0] == "ffmpeg"
        assert kwargs["check"]
        assert kwargs["timeout"] > 0
        assert Path(argv[argv.index("-i") + 1]).read_bytes() == b"source"
        label = argv[argv.index("-vf") + 1]
        assert label.startswith("drawtext=")
        assert "KI-generierter Avatar" in label
        assert "y=h*0.04" in label
        assert "box=1" in label
        assert argv[argv.index("-c:a") + 1] == "copy"
        Path(argv[-1]).write_bytes(b"labelled")

    monkeypatch.setattr(subprocess, "run", run)
    assert burn_ai_label(b"source") == b"labelled"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is unavailable")
def test_burn_label_with_ffmpeg(tmp_path):
    source = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=180x320:rate=5",
            "-t",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    original = source.read_bytes()
    labelled = burn_ai_label(original)
    assert original != labelled
    assert labelled[4:8] == b"ftyp"
