"""Render progress: a live bar, throttled log lines, and a JSON snapshot.

Three surfaces, because a render is watched in three different ways:

* **A terminal.** One bar on the last line, redrawn in place with ``\\r``.
* **A pipe.** Hermes jobs, cron and ``tee`` all give stdout a pipe, where a
  carriage-return bar is invisible. There it degrades to throttled plain lines
  that survive ``grep`` and land in ``render.log``.
* **Another process.** ``work/progress.json`` is rewritten atomically on every
  clip, so a monitor (``forge watch``, a dashboard, a chat bot) can read the
  state of a render it did not start.

The ETA is measured, not assumed. Wall time per clip differs by 5x between
hosts and engine builds on this project (2.3s on one run, 13.7s on another with
the same config), so a constant would be wrong most of the time. The bar takes
the median wall time of the last N completed clips and multiplies by the clips
still to render, which self-corrects after the first window and never counts
cached takes as work left.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable, TextIO

SAMPLES = 20
"""Clips remembered for the ETA median. Long enough to ride out one slow take."""


def human(seconds: float) -> str:
    """Compact duration: `9m12s`, `1h04m`, `13s`."""
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


_hms = human


def bar(fraction: float, width: int) -> str:
    """ASCII fill bar. No unicode, so it survives ssh, tmux and log files."""
    width = max(1, width)
    filled = int(round(width * min(1.0, max(0.0, fraction))))
    return "#" * filled + "-" * (width - filled)


class Progress:
    """Per-section clip counter with an ETA and three output surfaces.

    ``total`` is every chunk in the section. ``done_at_start`` is the takes a
    previous run already rendered, so a resumed chapter opens at the right
    position and the ETA only covers real work.
    """

    def __init__(self, *, label: str, total: int, done_at_start: int = 0,
                 section_no: int | None = None, section: int | None = None,
                 sections: int | None = None, enabled: bool = True,
                 state_path: Path | None = None,
                 stream: TextIO | None = None,
                 throttle: float = 60.0,
                 line_sink: Callable[[str], None] | None = None):
        self.label = label
        self.total = max(0, int(total))
        self.done = min(self.total, max(0, int(done_at_start)))
        self.section_no = section_no
        self.section = section
        self.sections = sections
        self.enabled = enabled
        self.state_path = Path(state_path) if state_path else None
        self.stream = stream or sys.stdout
        self.throttle = throttle
        self.line_sink = line_sink

        self.phase = "render"
        self.samples: deque[float] = deque(maxlen=SAMPLES)
        self.audio_seconds = 0.0
        self.started = time.monotonic()
        self._last_clip: float | None = None
        self._last_emit = 0.0
        self._bar_shown = False
        self._finished = False
        self.warned: str | None = None

    # ------------------------------------------------------------- surfaces
    @property
    def tty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except (AttributeError, ValueError):
            return False

    def begin(self, note: str = "") -> None:
        """Open the section: one line, then the bar in a terminal."""
        self.started = time.monotonic()
        self._last_clip = self.started
        self._write_state()
        if not self.enabled:
            return
        if note:
            self._line(f"PROG {self.label}: {note}")
        if self.tty:
            self._draw()

    def note(self, msg: str) -> None:
        """A plain line that must not be swallowed by the bar."""
        if not self.enabled:
            return
        self._line(f"PROG {self.label}: {msg}")

    def suspend(self) -> None:
        """Wipe the bar so another writer's line does not land on top of it."""
        if self.enabled and self.tty:
            self._clear()

    def resume(self) -> None:
        """Redraw the bar after a foreign line, if clips are still rendering."""
        if (self.enabled and self.tty and not self._finished
                and not self._bar_shown and self.phase == "render"):
            self._draw()

    def phase_to(self, phase: str, note: str = "") -> None:
        self.phase = phase
        self._write_state()
        if not self.enabled:
            return
        if self.tty:
            self._clear()
        if note:
            self._line(f"PROG {self.label}: {note}")

    def clip(self, audio_seconds: float | None = None, index: int | None = None) -> None:
        """Record one finished clip."""
        now = time.monotonic()
        self.done = min(self.total, self.done + 1)
        if index is not None and index + 1 > self.done:
            self.done = min(self.total, index + 1)
        if self._last_clip is not None:
            self.samples.append(max(0.0, now - self._last_clip))
        self._last_clip = now
        if audio_seconds:
            self.audio_seconds += float(audio_seconds)
        self._write_state(now)
        if self.enabled:
            self._emit(now)

    def finish(self, note: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        self.phase = "done"
        self._write_state()
        if not self.enabled:
            return
        if self.tty:
            self._clear()
        self._line(f"PROG {self.label}: {self.done}/{self.total} clips in "
                   f"{_hms(time.monotonic() - self.started)}"
                   + (f", {note}" if note else ""))

    # --------------------------------------------------------------- numbers
    @property
    def per_clip(self) -> float | None:
        if len(self.samples) < 3:
            return None
        return statistics.median(self.samples)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def eta(self) -> float | None:
        per = self.per_clip
        return None if per is None else per * self.remaining

    def _bar_width(self) -> int:
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        return max(8, cols - len(self._prefix()) - 44)

    def _prefix(self) -> str:
        tag = (f"sec {self.section}/{self.sections} "
               if self.section and self.sections and self.sections > 1 else "")
        return f"{tag}{self.label}"[:34]

    def _eta_piece(self) -> str:
        per = self.per_clip
        if per is None:
            return "warming up"
        return f"{_hms(per * self.remaining)} left, {per:.1f}s/clip"

    # --------------------------------------------------------------- drawing
    def _emit(self, now: float) -> None:
        if self.tty:
            self._draw()
            return
        if now - self._last_emit < self.throttle and self.done < self.total:
            return
        self._last_emit = now
        pct = 100.0 * self.done / self.total if self.total else 100.0
        self._line(f"PROG {self.label}: {self.done}/{self.total} clips "
                   f"({pct:.0f}%) {_hms(now - self.started)} elapsed, "
                   f"{self._eta_piece()}")

    def _draw(self) -> None:
        width = self._bar_width()
        pct = 100.0 * self.done / self.total if self.total else 100.0
        line = (f"{self._prefix()} [{bar(self.done / self.total if self.total else 1, width)}] "
                f"{self.done}/{self.total} {pct:3.0f}% "
                f"{_hms(time.monotonic() - self.started)} {self._eta_piece()}")
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        line = line[:max(20, cols - 1)]
        self.stream.write("\r" + line.ljust(cols - 1))
        self.stream.flush()
        self._bar_shown = True

    def _clear(self) -> None:
        if not self._bar_shown:
            return
        try:
            cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        except OSError:
            cols = 80
        self.stream.write("\r" + " " * (cols - 1) + "\r")
        self.stream.flush()
        self._bar_shown = False

    def _line(self, msg: str) -> None:
        if self.tty:
            self._clear()
        if self.line_sink:
            self.line_sink(msg)
        else:
            self.stream.write(msg + "\n")
            self.stream.flush()
        if self.tty and not self._finished and not self.line_sink:
            self._draw()

    # ----------------------------------------------------------------- state
    def snapshot(self) -> dict:
        per = self.per_clip
        return {
            "label": self.label,
            "phase": self.phase,
            "section": self.section,
            "sections": self.sections,
            "clipsDone": self.done,
            "clipsTotal": self.total,
            "clipsRemaining": self.remaining,
            "percent": round(100.0 * self.done / self.total, 1) if self.total else 100.0,
            "elapsedSeconds": round(time.monotonic() - self.started, 1),
            "etaSeconds": round(self.eta, 1) if self.eta is not None else None,
            "secondsPerClip": round(per, 2) if per else None,
            "audioSeconds": round(self.audio_seconds, 1),
            "updated": round(time.time(), 1),
        }

    def _write_state(self, _now: float | None = None) -> None:
        if not self.enabled or not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.snapshot(), indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass
