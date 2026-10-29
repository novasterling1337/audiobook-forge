"""ffmpeg / ffprobe helpers. No book-specific knowledge lives here."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from collections.abc import Sequence

FFMPEG = os.environ.get("FORGE_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FORGE_FFPROBE", "ffprobe")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def duration(path: str | os.PathLike) -> float:
    r = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_audio(path: str | os.PathLike) -> bool:
    return duration(path) > 0.05


def silence(path: str | os.PathLike, seconds: float, sample_rate: int = 24000) -> Path:
    """Create (once) a mono silence file used as the inter-paragraph gap."""
    p = Path(path)
    if p.exists() and duration(p) > 0:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    r = run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
             "-i", f"anullsrc=r={sample_rate}:cl=mono", "-t", f"{seconds}",
             "-c:a", "pcm_s16le", str(p)])
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not create silence: {r.stderr[-300:]}")
    return p


def concat(entries: Sequence[str | os.PathLike], out: str | os.PathLike,
           list_file: str | os.PathLike | None = None) -> Path:
    """Stream-copy concatenate in the given order."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = Path(list_file) if list_file else out.with_suffix(".concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(f"file '{Path(e).as_posix()}'\n")
    tmp = out.with_name(f"{out.stem}.part{out.suffix}")
    r = run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", str(tmp)])
    if r.returncode != 0:
        raise RuntimeError(f"concat failed: {r.stderr[-400:]}")
    os.replace(tmp, out)
    return out


def levels(path: str | os.PathLike) -> dict:
    r = run([FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect",
             "-f", "null", os.devnull])
    out = {}
    for line in r.stderr.splitlines():
        if "mean_volume:" in line:
            out["mean_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
        elif "max_volume:" in line:
            out["max_db"] = float(line.split("max_volume:")[1].split("dB")[0])
    return out


def probe(path: str | os.PathLike) -> dict:
    r = run([FFPROBE, "-v", "error", "-show_streams", "-show_format",
             "-of", "json", str(path)])
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def ffmetadata(entries: list[tuple[str, str, float]], title: str, author: str = "") -> str:
    """Build an ffmetadata blob with one chapter marker per section."""
    lines = [";FFMETADATA1", f"title={title}"]
    if author:
        lines.append(f"artist={author}")
    lines.append(f"album={title}")
    lines.append("genre=Audiobook")
    t = 0.0
    for chapter_title, _path, dur in entries:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={int(round(t * 1000))}",
                  f"END={int(round((t + dur) * 1000))}",
                  f"title={chapter_title}"]
        t += dur
    return "\n".join(lines) + "\n"


def encode_m4b(entries: list[tuple[str, str, float]], out: str | os.PathLike,
               title: str, author: str = "", bitrate: str = "64k",
               source_wav: str | os.PathLike | None = None,
               metadata_file: str | os.PathLike | None = None,
               dry_run: bool = False) -> Path:
    """Encode one chapterised .m4b.

    `entries` is [(chapter_title, wav_path, duration_seconds)] in order.
    When `source_wav` is None the wavs are concatenated first; pass a joined
    file to re-encode without touching the sources. `dry_run` writes only the
    metadata file and skips encoding.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = Path(metadata_file) if metadata_file else out.with_suffix(".ffmeta.txt")
    meta.write_text(ffmetadata(entries, title, author), encoding="utf-8")
    if dry_run:
        return meta

    if source_wav is None:
        joined = out.with_suffix(".joined.wav")
        concat([p for _t, p, _d in entries], joined,
               list_file=out.with_suffix(".joined.txt"))
        source_wav = joined

    r = run([FFMPEG, "-y", "-v", "error", "-i", str(source_wav),
             "-i", str(meta), "-map_metadata", "1", "-map", "0:a",
             "-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart",
             "-f", "mp4", str(out)])
    if r.returncode != 0:
        raise RuntimeError(f"m4b encode failed: {r.stderr[-400:]}")
    if source_wav != out and Path(source_wav).name.endswith(".joined.wav"):
        Path(source_wav).unlink(missing_ok=True)
    return out


def which(name: str) -> str | None:
    return shutil.which(name)
