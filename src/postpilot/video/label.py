import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

AI_AVATAR_CAPTION_LABEL = "KI-generierter Avatar / AI-generated avatar"

# Fontconfig resolves the font family on the runner without a fixed font file path.
_FILTER = (
    "drawtext=font=DejaVu Sans:text=KI-generierter Avatar:"
    "x=w*0.04:y=h*0.04:fontsize=h*0.035:fontcolor=white:box=1:"
    "boxcolor=black@0.8:boxborderw=6"
)


def burn_ai_label(video: bytes) -> bytes:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "source.mp4"
        output = Path(directory) / "labelled.mp4"
        source.write_bytes(video)
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-i",
                str(source),
                "-vf",
                _FILTER,
                "-c:v",
                "libx264",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        return output.read_bytes()
