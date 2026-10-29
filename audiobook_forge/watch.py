"""Follow a render from outside the process doing it.

`forge render` already reports its own progress. This exists for the cases
where you cannot see that output: a render started by a job runner or a
scheduler, a chapter running on another machine over ssh, or a chat bot that
wants to answer "how far along is it" without attaching to anything.

Read-only by construction. Nothing here writes a file, spawns a render or
touches a take, so it is safe to run next to a live render, and safe to kill.

Two sources, in order of quality:

1. `work/<slug>/progress.json`, written by the renderer per clip. Authoritative
   while it is fresh, and it carries phases the filesystem cannot show
   (`joining`, `failed`).
2. The filesystem, when that snapshot is missing or stale: finished takes in
   `work/secNN/take_NNNNN.wav` plus any takes sitting in an in-flight window
   directory. Per-clip wall time comes from the takes' mtimes, so an ETA works
   even on a render that predates the snapshot.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from . import audio, chunk
from .config import Book
from .progress import bar, human
from .render import slug_for

STALE = 150.0
"""Seconds after which a snapshot is treated as abandoned rather than live."""


def _takes(cdir: Path) -> list[Path]:
    return sorted(cdir.glob("take_*.wav"))


def _clip_mtimes(cdir: Path) -> list[float]:
    """Completion timestamps of every visible clip, adopted or still in a window.

    Vocello writes each wav as its clip finishes, and `os.replace` keeps the
    mtime, so both sets are honest completion times. Needed because a section
    that fits in one window has no adopted takes until the window ends.
    """
    times = [p.stat().st_mtime for p in _takes(cdir)]
    for wdir in cdir.glob("w*_*"):
        if wdir.is_dir():
            times += [p.stat().st_mtime for p in wdir.glob("*_clone_*.wav")]
    return sorted(times)


def _inflight(cdir: Path) -> int:
    """Best count of clips finished inside a window that has not been adopted."""
    best = 0
    for wdir in cdir.glob("w*_*"):
        if wdir.is_dir():
            best = max(best, len(list(wdir.glob("*_clone_*.wav"))))
    return best


def _seconds_per_clip(cdir: Path) -> float | None:
    """Median gap between recent clip completions, so an old run still gets an ETA."""
    times = _clip_mtimes(cdir)
    gaps = [b - a for a, b in zip(times, times[1:])]
    gaps = [g for g in gaps if 0.2 < g < 900]
    if len(gaps) < 2:
        return None
    return statistics.median(gaps[-20:])


def _snapshot_file(book: Book) -> dict | None:
    path = book.work_dir / "progress.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def section_state(book: Book, section: dict) -> dict:
    """Everything known about one section, from the snapshot or the filesystem."""
    label = slug_for(section)
    final = book.out_dir / f"{section['no']:02d} {label}.wav"
    plan = chunk.plan(section, int(book.render["chunk_chars"]), float(book.render["wpm"]))
    total = len(plan["chunks"])
    cdir = book.work_dir / f"sec{section['no']:02d}"

    state = {
        "no": section["no"],
        "label": label,
        "clipsTotal": total,
        "clipsDone": 0,
        "phase": "pending",
        "etaSeconds": None,
        "secondsPerClip": None,
        "audioSeconds": 0.0,
        "live": False,
        "rendered": False,
        "minutes": 0.0,
    }

    if final.exists() and audio.has_audio(final):
        state.update(phase="done", rendered=True, clipsDone=total,
                     minutes=audio.duration(final) / 60)
        state["audioSeconds"] = state["minutes"] * 60
        return state

    snap = _snapshot_file(book)
    if snap and snap.get("label") == label:
        age = time.time() - float(snap.get("updated") or 0)
        state["clipsDone"] = int(snap.get("clipsDone") or 0)
        state["phase"] = snap.get("phase") or "render"
        state["audioSeconds"] = float(snap.get("audioSeconds") or 0)
        if age < STALE:
            # Position comes from the snapshot (per clip, phase-aware). The rate
            # can come from either: the renderer needs three clips before it
            # quotes one, and the file mtimes are the same events anyway.
            per = snap.get("secondsPerClip") or _seconds_per_clip(cdir)
            eta = snap.get("etaSeconds")
            if eta is None and per is not None:
                eta = per * max(0, total - state["clipsDone"])
            state.update(live=True, etaSeconds=eta, secondsPerClip=per)
            return state

    take_files = _takes(cdir)
    done = len(take_files)
    per = _seconds_per_clip(cdir)
    inflight = _inflight(cdir)
    state["clipsDone"] = min(total, done + inflight)
    state["secondsPerClip"] = per
    if done or inflight:
        state["phase"] = "render"
    if per is not None:
        state["etaSeconds"] = per * max(0, total - state["clipsDone"])
    # No audio total here on purpose: summing takes means one ffprobe per clip,
    # which is far too slow for a two-second refresh loop. Estimates use clips.
    return state


def book_state(book: Book) -> list[dict]:
    return [section_state(book, s) for s in book.body_sections()]


def line_for(state: dict) -> str:
    """One line per section for the non-following listing."""
    pct = 100.0 * state["clipsDone"] / state["clipsTotal"] if state["clipsTotal"] else 0.0
    if state["rendered"]:
        detail = f"{state['minutes']:.1f} min"
    elif state["phase"] in ("pending",):
        detail = "not started"
    else:
        detail = f"{state['clipsDone']}/{state['clipsTotal']} clips ({pct:.0f}%)"
        if state["etaSeconds"]:
            detail += f", {human(state['etaSeconds'])} left"
        if state["live"]:
            detail += " [live]"
    return (f"{state['no']:02d}  {state['label'][:44]:<46}"
            f"{state['phase']:<8}{detail}")


def render_bar(state: dict, columns: int = 80) -> str:
    """The single-line bar used in follow mode."""
    total = max(1, state["clipsTotal"])
    pct = 100.0 * state["clipsDone"] / total
    prefix = state["label"][:34]
    width = max(8, columns - len(prefix) - 44)
    tail = ""
    if state["etaSeconds"]:
        tail = f", {human(state['etaSeconds'])} left"
    if state["secondsPerClip"]:
        tail += f", {state['secondsPerClip']:.1f}s/clip"
    if state["live"]:
        tail += " [live]"
    return (f"{prefix} [{bar(state['clipsDone'] / total, width)}] "
            f"{state['clipsDone']}/{state['clipsTotal']} {pct:3.0f}%{tail}")
