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
import time
from pathlib import Path

from . import audio, chunk
from .config import Book, ConfigError

VOCELLO = os.environ.get("FORGE_VOCELLO", "vocello")


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
                 out_dir: Path | None = None):
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

    # ------------------------------------------------------------------ log
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

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
        """Recover takes from a crashed run that never wrote state.json."""
        orphans = sorted(p for p in cdir.glob("take_*.wav") if audio.has_audio(p))
        takes = {}
        for p in orphans:
            m = re.search(r"take_(\d+)\.wav$", p.name)
            if not m:
                continue
            i = int(m.group(1))
            if i < len(texts):
                takes[i] = {"h": _hash(texts[i]), "p": str(p), "dur": round(audio.duration(p), 3)}
        if takes:
            self.log(f"  adopted {len(takes)} takes left by an interrupted run")
            self._save_state(cdir, fingerprint, takes)
        return takes

    def _run_window(self, texts: list[str], wdir: Path) -> dict[int, Path]:
        wdir.mkdir(parents=True, exist_ok=True)
        rows = wdir / "rows.txt"
        rows.write_text("\n".join(t.replace("\n", " ") for t in texts) + "\n", encoding="utf-8")

        cmd = [VOCELLO, "batch", "--file", str(rows), "--mode", self.mode,
               "--voice", self.voice, "--confirm-consent",
               "--out-dir", str(wdir), "--json"]
        cmd += list(self.book.voice.get("args", []))
        r = subprocess.run(cmd, capture_output=True, text=True)

        got: dict[int, Path] = {}
        for f in sorted(wdir.glob("*_clone_*.wav")):
            m = re.search(r"_clone_(\d+)\.wav$", f.name)
            if m and audio.has_audio(f):
                got[int(m.group(1))] = f
        if r.returncode != 0:
            tail = " | ".join((r.stderr or "").strip().splitlines()[-2:])[:220]
            self.log(f"  window returned {r.returncode} ({len(got)}/{len(texts)} clips): {tail}")
        return {i: p for i, p in got.items() if i < len(texts)}

    # -------------------------------------------------------------- render
    def render_section(self, section: dict) -> bool:
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
        takes = self._load_state(cdir, fingerprint) or self._adopt_orphans(cdir, texts, fingerprint)
        missing = [i for i in range(len(texts)) if i not in takes]

        self.log(f"START {slug_for(section)}: {p['paragraphs']} paragraphs, {words} words, "
                 f"{len(chunks)} chunks ({len(takes)} cached), expect ~{expected/60:.1f} min audio")
        t0 = time.time()

        for win_start in range(0, len(missing), self.window):
            window = missing[win_start:win_start + self.window]
            todo = list(window)
            attempt = 0
            while todo and attempt < self.attempts:
                attempt += 1
                wdir = cdir / f"w{win_start:05d}_{attempt}"
                got = self._run_window([texts[i] for i in todo], wdir)
                for local_i, path in got.items():
                    gidx = todo[local_i]
                    dest = cdir / f"take_{gidx:05d}.wav"
                    os.replace(path, dest)
                    takes[gidx] = {"h": _hash(texts[gidx]), "p": str(dest),
                                   "dur": round(audio.duration(dest), 3)}
                shutil.rmtree(wdir, ignore_errors=True)
                todo = [i for i in todo if i not in takes]
                self._save_state(cdir, fingerprint, takes)
                if todo:
                    self.log(f"  window {win_start//self.window} attempt {attempt}: "
                             f"{len(todo)} rows still missing")
            if todo:
                self.log(f"FAIL {slug_for(section)}: {len(todo)} rows would not render "
                         f"after {self.attempts} attempts: {todo[:8]}")
                return False

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

        if not self.keep_takes:
            shutil.rmtree(cdir, ignore_errors=True)
        return True

    def render_all(self, sections: list[dict]) -> bool:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"=== render start: {len(sections)} sections, voice '{self.voice}', "
                 f"window {self.window}, {self.chunk_chars} chars/clip ===")
        ok = True
        for sec in sections:
            ok = self.render_section(sec) and ok
        self.log(f"=== render finished: {'all sections ok' if ok else 'WITH FAILURES'} ===")
        return ok
