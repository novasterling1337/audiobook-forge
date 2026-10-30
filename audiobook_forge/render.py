"""Render sections to audio with the Vocello CLI.

Three design points, each of them a scar:

1. **Windows, not one big batch.** A single `vocello batch` over 140 clips
   aborted twice with `batch_incomplete` (103/140, then 83/108) and no error
   worth reading. Rendering in windows of N clips, each a fresh process, and
   retrying only the rows that are still missing keeps a wedged engine from
   poisoning a whole chapter.

2. **Resume at clip granularity.** Every completed take is moved to a stable
   name (`take_00042.wav`) and recorded in `state.json` with a hash of its
   source text. A crash costs the unfinished rows, and a changed paragraph
   invalidates only that clip. Each window renders into its own directory
   first, so vocello's `_clone_NNN` names can never collide across windows.

3. **Duration is the verification signal.** A whisper pass over an hour of
   audio can silently skip a paragraph and look fine; a word-count model
   against the real file duration catches truncation and stalling.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, TextIO

from . import audio, chunk
from .config import Book, ConfigError
from .progress import Progress

VOCELLO = os.environ.get("FORGE_VOCELLO", "vocello")

_NDJSON: bool | None = None


def supports_ndjson() -> bool:
    """Does this vocello stream per-clip events?

    `--ndjson` is how progress gets clip granularity. It exists in 3.x, and
    older builds only know `--json`, so the render still works and the bar
    degrades to one step per window instead of failing mid-chapter.
    """
    global _NDJSON
    if _NDJSON is None:
        try:
            r = subprocess.run([VOCELLO, "capabilities"], capture_output=True,
                               text=True, timeout=30)
            data = json.loads(r.stdout or "{}")
            _NDJSON = "ndjson" in (data.get("outputFormats") or [])
        except (OSError, ValueError, subprocess.SubprocessError):
            _NDJSON = False
    return _NDJSON


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def slug_for(section: dict) -> str:
    """Filesystem-safe label: `04 Chapter 4 - Machinations in the Matrix of Time`."""
    name = section["name"] + (f" - {section['title']}" if section.get("title") else "")
    return re.sub(r"[^\w\s\-]", "", name).strip()


class Renderer:
    def __init__(self, book: Book, *, voice: str | None = None,
                 window: int | None = None, chunk_chars: int | None = None,
                 keep_takes: bool | None = None, dry_run: bool = False,
                 out_dir: Path | None = None, progress: bool = True,
                 stream: TextIO | None = None):
        self.book = book
        self.voice = voice or book.voice.get("name") or ""
        self.mode = book.voice.get("mode", "clone")
        self.window = int(window or book.render["window"])
        self.chunk_chars = int(chunk_chars or book.render["chunk_chars"])
        self.attempts = int(book.render["attempts"])
        self.wpm = float(book.render["wpm"])
        self.tolerance = float(book.render["tolerance"])
        self.keep_takes = bool(book.render["keep_takes"] if keep_takes is None else keep_takes)
        self.dry_run = dry_run
        self.out_dir = out_dir or book.out_dir
        self.work = book.work_dir
        self.log_path = book.log_file
        self.progress = progress
        self.stream = stream or sys.stdout
        self._prog: Progress | None = None

    # ------------------------------------------------------------------ log
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        if self._prog is not None:
            self._prog.suspend()
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self._prog is not None:
            self._prog.resume()

    # ---------------------------------------------------------------- paths
    def final_path(self, section: dict) -> Path:
        return self.out_dir / f"{section['no']:02d} {slug_for(section)}.wav"

    def manifest_path(self, section: dict) -> Path:
        return self.out_dir / f"{section['no']:02d} {slug_for(section)}.json"

    def take_dir(self, section: dict) -> Path:
        return self.work / f"sec{section['no']:02d}"

    # -------------------------------------------------------------- helpers
    def gaps(self) -> tuple[Path, Path]:
        sr = 24000
        para = audio.silence(self.work / "gap_para.wav", float(self.book.render["paragraph_gap"]), sr)
        inner = audio.silence(self.work / "gap_inner.wav", float(self.book.render["inner_gap"]), sr)
        return para, inner

    def _load_state(self, cdir: Path, fingerprint: str) -> dict[int, dict]:
        path = cdir / "state.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if data.get("fingerprint") != fingerprint:
            return {}
        takes = {}
        for k, v in data.get("takes", {}).items():
            if Path(v["p"]).exists() and audio.has_audio(v["p"]):
                takes[int(k)] = v
        return takes

    def _save_state(self, cdir: Path, fingerprint: str, takes: dict[int, dict]) -> None:
        payload = {"fingerprint": fingerprint,
                   "takes": {str(k): v for k, v in sorted(takes.items())}}
        (cdir / "state.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    def _adopt_orphans(self, cdir: Path, texts: list[str], fingerprint: str) -> dict[int, dict]:
        """Recover takes from a crashed run that never wrote state.json.

        A take is adopted by index only when it carries the matching text hash
        in its `.h` sidecar. Legacy takes without a sidecar are adopted as-is,
        which is why the caller only calls this when there is no state.json at
        all: after a text change the fingerprint no longer matches and every
        take on disk is provably stale.
        """
        orphans = sorted(p for p in cdir.glob("take_*.wav") if audio.has_audio(p))
        takes = {}
        for p in orphans:
            m = re.search(r"take_(\d+)\.wav$", p.name)
            if not m:
                continue
            i = int(m.group(1))
            if i >= len(texts):
                continue
            sidecar = p.with_suffix(".h")
            if sidecar.exists():
                if sidecar.read_text(encoding="utf-8").strip() != _hash(texts[i]):
                    continue
            takes[i] = {"h": _hash(texts[i]), "p": str(p), "dur": round(audio.duration(p), 3)}
        if takes:
            self.log(f"  adopted {len(takes)} takes left by an interrupted run")
            self._save_state(cdir, fingerprint, takes)
        return takes

    def _spawn(self, cmd: list[str],
               on_clip: Callable[[float | None], None] | None) -> tuple[int, list[str]]:
        """Run one vocello window, streaming events, keeping stderr for errors.

        stderr is drained on its own thread: the engine logs a few lines on
        model load, and a full stderr pipe would deadlock a long window.
        """
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        tail: deque[str] = deque(maxlen=4)

        def drain() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    tail.append(line)

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "result":
                # vocello reports usage and engine failures in the result event
                # on stdout, so without this the "window returned 66" line has
                # no reason attached to it.
                message = ((event.get("data") or {}).get("error") or {}).get("message")
                if message:
                    tail.append(message)
                continue
            if on_clip is not None and kind == "generation_completed":
                on_clip((event.get("data") or {}).get("durationSeconds"))

        proc.wait()
        thread.join(timeout=10)
        return proc.returncode, list(tail)

    def _run_window(self, texts: list[str], wdir: Path,
                    on_clip: Callable[[float | None], None] | None = None) -> dict[int, Path]:
        wdir.mkdir(parents=True, exist_ok=True)
        rows = wdir / "rows.txt"
        rows.write_text("\n".join(t.replace("\n", " ") for t in texts) + "\n", encoding="utf-8")

        streaming = on_clip is not None and supports_ndjson()
        cmd = [VOCELLO, "batch", "--file", str(rows), "--mode", self.mode,
               "--voice", self.voice, "--confirm-consent",
               "--out-dir", str(wdir), "--ndjson" if streaming else "--json"]
        cmd += list(self.book.voice.get("args", []))
        code, err_tail = self._spawn(cmd, on_clip if streaming else None)

        got: dict[int, Path] = {}
        for f in sorted(wdir.glob("*_clone_*.wav")):
            m = re.search(r"_clone_(\d+)\.wav$", f.name)
            if m and audio.has_audio(f):
                got[int(m.group(1))] = f
        if code != 0:
            tail = " | ".join(err_tail[-2:])[:220]
            self.log(f"  window returned {code} ({len(got)}/{len(texts)} clips): {tail}")
        return {i: p for i, p in got.items() if i < len(texts)}

    # -------------------------------------------------------------- render
    def render_section(self, section: dict, position: tuple[int, int] | None = None) -> bool:
        final = self.final_path(section)
        p = chunk.plan(section, self.chunk_chars, self.wpm)
        chunks, words, expected = p["chunks"], p["words"], p["expected_seconds"]
        texts = [c[0] for c in chunks]

        if final.exists() and audio.has_audio(final):
            d = audio.duration(final)
            drift = (d - expected) / expected if expected else 0.0
            if abs(drift) <= self.tolerance:
                self.log(f"SKIP {slug_for(section)}: already rendered ({d/60:.1f} min)")
                return True
            self.log(f"REDO {slug_for(section)}: {d/60:.1f} min on disk vs "
                     f"{expected/60:.1f} expected ({drift:+.0%})")

        if self.dry_run:
            self.log(f"PLAN {slug_for(section)}: {p['paragraphs']} paragraphs, "
                     f"{words} words, {len(chunks)} chunks, ~{expected/60:.1f} min audio")
            return True

        if not self.voice:
            raise ConfigError("no voice configured (book.toml [voice] name = ...)")

        cdir = self.take_dir(section)
        cdir.mkdir(parents=True, exist_ok=True)
        fingerprint = _hash("\x00".join(texts))
        takes = self._load_state(cdir, fingerprint)
        if not takes and not (cdir / "state.json").exists():
            # Only after a lost state.json. A state.json whose fingerprint no
            # longer matches means the section text changed, so the takes on
            # disk are stale audio, not recoverable work.
            takes = self._adopt_orphans(cdir, texts, fingerprint)
        missing = [i for i in range(len(texts)) if i not in takes]

        self.log(f"START {slug_for(section)}: {p['paragraphs']} paragraphs, {words} words, "
                 f"{len(chunks)} chunks ({len(takes)} cached), expect ~{expected/60:.1f} min audio")
        t0 = time.time()

        prog = Progress(
            label=slug_for(section), total=len(chunks), done_at_start=len(takes),
            section_no=section.get("no"),
            section=position[0] if position else None,
            sections=position[1] if position else None,
            enabled=self.progress, stream=self.stream,
            state_path=self.work / "progress.json", line_sink=self.log)
        self._prog = prog
        if len(takes):
            prog.note(f"{len(takes)} clips already cached, {len(missing)} to render")
        prog.begin()

        def on_clip(duration: float | None) -> None:
            prog.clip(duration)

        for win_start in range(0, len(missing), self.window):
            window = missing[win_start:win_start + self.window]
            todo = list(window)
            attempt = 0
            while todo and attempt < self.attempts:
                attempt += 1
                wdir = cdir / f"w{win_start:05d}_{attempt}"
                got = self._run_window([texts[i] for i in todo], wdir, on_clip=on_clip)
                for local_i, path in got.items():
                    gidx = todo[local_i]
                    dest = cdir / f"take_{gidx:05d}.wav"
                    os.replace(path, dest)
                    h = _hash(texts[gidx])
                    # sidecar so a later orphan adoption can prove the take
                    # belongs to this text and not to an older revision
                    dest.with_suffix(".h").write_text(h, encoding="utf-8")
                    takes[gidx] = {"h": h, "p": str(dest),
                                   "dur": round(audio.duration(dest), 3)}
                shutil.rmtree(wdir, ignore_errors=True)
                todo = [i for i in todo if i not in takes]
                self._save_state(cdir, fingerprint, takes)
                if todo:
                    prog.note(f"window {win_start//self.window} attempt {attempt}: "
                              f"{len(todo)} rows still missing")
            if todo:
                prog.phase_to("failed", f"{len(todo)} rows would not render")
                self._prog = None
                self.log(f"FAIL {slug_for(section)}: {len(todo)} rows would not render "
                         f"after {self.attempts} attempts: {todo[:8]}")
                return False

        prog.phase_to("joining", f"joining {len(chunks)} takes")
        gap_para, gap_inner = self.gaps()
        entries: list[Path] = []
        for n in range(len(chunks)):
            entries.append(Path(takes[n]["p"]))
            if n < len(chunks) - 1:
                entries.append(gap_para if chunks[n][1] else gap_inner)

        joined = audio.concat(entries, final, list_file=cdir / "join.txt")
        d = audio.duration(joined)
        drift = (d - expected) / expected if expected else 0.0
        tag = "WARN" if abs(drift) > self.tolerance else "OK"
        self.log(f"{tag} {slug_for(section)}: {d/60:.1f} min vs {expected/60:.1f} expected "
                 f"({drift:+.1%})")

        shorts = []
        for i in range(len(chunks)):
            iw = len(texts[i].split())
            if iw > 25 and takes[i]["dur"] < iw / self.wpm * 60 * 0.6:
                shorts.append([i, iw, takes[i]["dur"]])
        if shorts:
            self.log(f"WARN {slug_for(section)}: {len(shorts)} suspiciously short clips "
                     f"(index, words, sec): {shorts[:6]}")

        manifest = {
            "book": self.book.title,
            "section": slug_for(section),
            "voice": self.voice,
            "paragraphs": p["paragraphs"],
            "words": words,
            "chunks": len(chunks),
            "chunk_chars": self.chunk_chars,
            "wpm": self.wpm,
            "durationSeconds": round(d, 2),
            "expectedSeconds": round(expected, 1),
            "driftPercent": round(drift * 100, 1),
            "wallSeconds": round(time.time() - t0, 1),
            "shortClips": shorts,
            "takes": [{"index": i, "durationSeconds": takes[i]["dur"],
                       "h": takes[i]["h"]} for i in range(len(chunks))],
        }
        self.manifest_path(section).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        self.log(f"CHAPTER DONE {slug_for(section)}: {d/60:.1f} min audio, {len(chunks)} chunks, "
                 f"{time.time()-t0:.0f}s wall -> {joined}")
        prog.finish(f"{d/60:.1f} min audio ({drift:+.1%})")
        self._prog = None

        if not self.keep_takes:
            shutil.rmtree(cdir, ignore_errors=True)
        return True

    def render_all(self, sections: list[dict]) -> bool:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if not self.progress:
            # A quiet run must not leave a stale snapshot claiming "done" for a
            # monitor that polls work/progress.json.
            (self.work / "progress.json").unlink(missing_ok=True)
        self.log(f"=== render start: {len(sections)} sections, voice '{self.voice}', "
                 f"window {self.window}, {self.chunk_chars} chars/clip ===")
        ok = True
        for n, sec in enumerate(sections, start=1):
            ok = self.render_section(sec, position=(n, len(sections))) and ok
        self.log(f"=== render finished: {'all sections ok' if ok else 'WITH FAILURES'} ===")
        return ok
