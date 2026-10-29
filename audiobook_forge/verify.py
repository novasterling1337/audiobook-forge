"""Post-render checks that a human would otherwise do by ear."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import audio
from .render import slug_for


def check(path: Path, expected_seconds: float, tolerance: float = 0.25) -> dict:
    d = audio.duration(path)
    drift = (d - expected_seconds) / expected_seconds if expected_seconds else 0.0
    res = {
        "path": str(path),
        "durationSeconds": round(d, 2),
        "expectedSeconds": round(expected_seconds, 1),
        "driftPercent": round(drift * 100, 1),
        "ok": bool(d > 0 and abs(drift) <= tolerance),
    }
    res.update(audio.levels(path))
    return res


def transcribe(path: Path, start: float = 0.0, length: float = 25.0,
               model: str | None = None) -> str:
    """Spot-check what the audio actually says, using whisper-cli."""
    exe = shutil.which("whisper-cli")
    if not exe:
        raise RuntimeError("whisper-cli not installed (brew install whisper-cpp)")
    model = model or os.environ.get("FORGE_WHISPER_MODEL") or "ggml-base.en.bin"
    if not Path(model).is_absolute() and not Path(model).exists():
        raise RuntimeError(f"whisper model not found: {model} (set FORGE_WHISPER_MODEL)")

    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"forge_check_{os.getpid()}.wav"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(path), "-t", str(length), "-ar", "16000", "-ac", "1",
            "-c:a", "pcm_s16le", str(tmp)]
    subprocess.run(cmd, check=True, capture_output=True)
    r = subprocess.run([exe, "-m", model, "-f", str(tmp), "-nt", "--no-prints"],
                       capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    return " ".join(r.stdout.split())


def section_report(book, section: dict) -> dict:
    path = book.out_dir / f"{section['no']:02d} {slug_for(section)}.wav"
    manifest = book.out_dir / f"{section['no']:02d} {slug_for(section)}.json"
    expected = None
    if manifest.exists():
        try:
            expected = json.loads(manifest.read_text())["expectedSeconds"]
        except (json.JSONDecodeError, KeyError):
            expected = None
    if expected is None:
        words = sum(len(p.split()) for p in section["paras"])
        expected = words / float(book.render["wpm"]) * 60
    if not path.exists():
        return {"section": slug_for(section), "path": str(path), "missing": True}
    out = check(path, expected, float(book.render["tolerance"]))
    out["section"] = slug_for(section)
    return out
