"""Command line interface: `forge <command> <book>`."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import audio, config, extract as ex, verify as vf, watch as wat
from .chunk import plan
from .config import ConfigError
from .render import Renderer, VOCELLO, slug_for


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _pick(book, selectors: list[str] | None) -> list[dict]:
    sections = book.body_sections()
    if not selectors:
        return sections
    wanted: list[dict] = []
    for sel in selectors:
        hits = [s for s in sections
                if s["name"].lower() == sel.lower()
                or str(s["no"]) == sel
                or sel.lower() in (s["name"] + " " + s.get("title", "")).lower()]
        if not hits:
            raise ConfigError(f"no section matches {sel!r}")
        for h in hits:
            if h not in wanted:
                wanted.append(h)
    return wanted


def _human(seconds: float) -> str:
    h, rem = divmod(int(round(seconds)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    print("tools")
    for tool, why in (("pandoc", "reading EPUB/DOCX/HTML"),
                      ("ffmpeg", "gaps, joining, encoding"),
                      ("ffprobe", "duration checks"),
                      (VOCELLO, "speech synthesis"),
                      ("whisper-cli", "optional transcript spot-checks")):
        path = shutil.which(tool)
        print(f"  {'ok  ' if path else 'MISS'} {tool:<12} {path or why}")

    if shutil.which(VOCELLO):
        r = subprocess.run([VOCELLO, "--version"], capture_output=True, text=True)
        print(f"  vocello version {r.stdout.strip() or '?'}")
        r = subprocess.run([VOCELLO, "voice", "list"], capture_output=True, text=True)
        voices = [ln.split("\t")[0].strip() for ln in r.stdout.splitlines() if ln.strip()]
        print(f"  voices: {', '.join(voices) if voices else 'none'}")

    try:
        book = config.load(args.book)
    except ConfigError as exc:
        print(f"\nbook: {exc}")
        return 0

    print(f"\nbook {book.slug}")
    print(f"  title    {book.describe()}")
    print(f"  source   {book.source or '(not set)'}")
    print(f"  out_dir  {book.out_dir}")
    print(f"  work_dir {book.work_dir}")
    print(f"  voice    {book.voice.get('name') or '(not set)'}")
    if book.voice.get("name") and shutil.which(VOCELLO):
        r = subprocess.run([VOCELLO, "voice", "list"], capture_output=True, text=True)
        have = [ln.split("\t")[0].strip() for ln in r.stdout.splitlines() if ln.strip()]
        print(f"  voice ok {book.voice['name'] in have}")
    if book.sections_file.exists():
        secs = book.load_sections()
        body = book.body_sections()
        words = sum(len(p.split()) for s in body for p in s["paras"])
        print(f"  sections {len(secs)} ({len(body)} to render), "
              f"{words} words ~ {_human(words / float(book.render['wpm']) * 60)}")
    else:
        print(f"  sections missing -> forge extract {book.slug}")
    du = shutil.disk_usage(book.out_dir if book.out_dir.exists() else Path.home())
    print(f"  disk     {du.free / 1e9:.0f} GB free")
    return 0


def cmd_books(args) -> int:
    root = config.books_root()
    rows = sorted(d for d in root.iterdir() if (d / "book.toml").exists()) if root.is_dir() else []
    if not rows:
        print(f"no books under {root}")
        return 0
    for d in rows:
        try:
            b = config.load(str(d))
            done = sum(1 for s in b.body_sections()
                       if (b.out_dir / f"{s['no']:02d} {slug_for(s)}.wav").exists())
            total = len(b.body_sections())
            print(f"{b.slug:<24} {done}/{total} sections  {b.title}")
        except ConfigError as exc:
            print(f"{d.name:<24} broken: {exc}")
    return 0


def cmd_new(args) -> int:
    root = config.books_root()
    dest = root / args.slug
    if (dest / "book.toml").exists() and not args.force:
        print(f"{dest} already exists (use --force)")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    src = str(Path(os.path.expanduser(args.source)).resolve()) if args.source else ""
    body = f'''# audiobook-forge book definition
slug = "{args.slug}"
title = "{args.title or args.slug.replace('-', ' ').title()}"
author = "{args.author or ''}"
source = "{src}"
out_dir = "{args.out_dir or f'~/Audiobooks/{args.slug}'}"

[voice]
name = "{args.voice or ''}"
mode = "clone"

[extract]
# everything before this line is dropped (title page, copyright, epigraph)
start_at = ""
# sections never rendered
exclude = ["Works Cited", "Notes", "Index", "Bibliography"]
# lines matching these regexes are deleted from the text
skip_lines = []

[render]
wpm = 180.0
chunk_chars = 850
window = 35
tolerance = 0.25
'''
    (dest / "book.toml").write_text(body, encoding="utf-8")
    print(f"created {dest / 'book.toml'}")
    print(f"next: edit it, then `forge extract {args.slug}`")
    return 0


def cmd_extract(args) -> int:
    book = config.load(args.book)
    src = Path(os.path.expanduser(args.source or book.source or ""))
    if not src.name:
        raise ConfigError("no source file; set `source` in book.toml or pass --source")
    start = args.start_at if args.start_at is not None else book.extract.get("start_at")
    end = args.end_at if args.end_at is not None else book.extract.get("end_at")
    skip = list(book.extract.get("skip_lines", [])) + list(args.skip or [])
    heading = (args.heading_pattern if args.heading_pattern is not None
               else book.extract.get("heading_pattern"))
    print(f"extracting {src.name} -> {book.sections_file}")
    sections = ex.extract(src, book.sections_file, start_at=start, end_at=end,
                          skip_lines=skip, heading_pattern=heading)
    words = sum(len(p.split()) for s in sections for p in s["paras"])
    print(f"{len(sections)} sections, {words} words, "
          f"~{_human(words / float(book.render['wpm']) * 60)} of audio")
    return 0


def cmd_sections(args) -> int:
    book = config.load(args.book)
    for s in book.load_sections():
        words = sum(len(p.split()) for p in s["paras"])
        mark = "render" if s in book.body_sections() else "  --  "
        print(f"{mark} {s['no']:02d}  {s['name']:<24} {s.get('title','')[:44]:<46}"
              f"{len(s['paras']):>4}p {words:>7}w {_human(words / float(book.render['wpm']) * 60):>8}")
    return 0


def cmd_render(args) -> int:
    book = config.load(args.book)
    sections = _pick(book, args.section)
    r = Renderer(book, voice=args.voice, window=args.window, chunk_chars=args.chunks,
                  keep_takes=args.keep_takes, dry_run=args.dry_run,
                  progress=getattr(args, "progress", True),
                  out_dir=Path(os.path.expanduser(args.out)).expanduser() if args.out else None)
    ok = r.render_all(sections)
    return 0 if ok else 1


def cmd_watch(args) -> int:
    book = config.load(args.book)
    sections = _pick(book, args.section)
    interval = max(0.5, float(args.interval))

    if args.json:
        rows = [wat.section_state(book, s) for s in sections]
        print(json.dumps({"book": book.title, "slug": book.slug, "sections": rows}, indent=1))
        return 0

    if not args.follow:
        print(f"{book.title}  ({book.slug})")
        rows = [wat.section_state(book, s) for s in sections]
        for state in rows:
            print(wat.line_for(state))
        done = sum(1 for r in rows if r["rendered"])
        print(f"\n{done}/{len(rows)} rendered")
        return 0

    tty = sys.stdout.isatty()
    try:
        while True:
            rows = [wat.section_state(book, s) for s in sections]
            active = next((r for r in rows if r["phase"] in ("render", "joining")), None)
            if active is None:
                pending = [r for r in rows if not r["rendered"]]
                if not pending:
                    if tty:
                        print()
                    print("all sections rendered")
                    return 0
                active = pending[0]
            line = wat.render_bar(active)
            if tty:
                cols = shutil.get_terminal_size(fallback=(80, 24)).columns
                sys.stdout.write("\r" + line[:cols - 1].ljust(cols - 1))
                sys.stdout.flush()
            else:
                print(line, flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        if tty:
            print()
        return 0


def cmd_status(args) -> int:
    book = config.load(args.book)
    total = 0.0
    done = 0
    print(f"{book.title}  ({book.slug})")
    print(f"{'':4} {'section':<46}{'state':<10}{'audio':>9}{'drift':>8}")
    for s in book.load_sections():
        path = book.out_dir / f"{s['no']:02d} {slug_for(s)}.wav"
        man = book.out_dir / f"{s['no']:02d} {slug_for(s)}.json"
        rendered = path.exists() and audio.has_audio(path)
        if s not in book.body_sections():
            state = "excluded"
        elif rendered:
            state = "done"
            done += 1
        else:
            state = "pending"
        d = audio.duration(path) if rendered else 0.0
        total += d
        drift = ""
        if rendered and man.exists():
            try:
                drift = f"{json.loads(man.read_text())['driftPercent']:+.1f}%"
            except (json.JSONDecodeError, KeyError):
                drift = ""
        print(f"{s['no']:02d}   {slug_for(s)[:45]:<46}{state:<10}"
              f"{(f'{d/60:.1f} min' if rendered else '-'):>9}{drift:>8}")
    body = book.body_sections()
    print(f"\n{done}/{len(body)} rendered, {_human(total)} of audio on disk")
    return 0


def cmd_verify(args) -> int:
    book = config.load(args.book)
    sections = _pick(book, args.section)
    failed = 0
    for s in sections:
        rep = vf.section_report(book, s)
        if rep.get("missing"):
            print(f"MISSING {rep['section']}")
            failed += 1
            continue
        flag = "ok  " if rep["ok"] else "WARN"
        print(f"{flag} {rep['section'][:52]:<54}{rep['durationSeconds']/60:6.1f} min  "
              f"{rep['driftPercent']:+6.1f}%  mean {rep.get('mean_db','?')} dB")
        if not rep["ok"]:
            failed += 1
        if args.transcribe and not rep.get("missing"):
            path = Path(rep["path"])
            head = vf.transcribe(path, 0, 22)
            tail = vf.transcribe(path, max(0, rep["durationSeconds"] - 25), 25)
            print(f"     head: {head[:160]}")
            print(f"     tail: {tail[-160:]}")
    return 0 if failed == 0 else 1


def cmd_bundle(args) -> int:
    book = config.load(args.book)
    entries = []
    for s in book.body_sections():
        path = book.out_dir / f"{s['no']:02d} {slug_for(s)}.wav"
        if not path.exists():
            continue
        entries.append((slug_for(s), str(path), audio.duration(path)))
    if not entries:
        print("nothing rendered yet")
        return 1
    out = Path(os.path.expanduser(args.out)) if args.out else \
        book.out_dir / f"{book.title} (unabridged).m4b"
    print(f"bundling {len(entries)} sections -> {out}")
    if args.dry_run:
        meta = out.with_suffix(".ffmeta.txt")
        audio.encode_m4b(entries, out, book.title, book.author,
                         bitrate=args.bitrate or book.bundle.get("bitrate", "64k"),
                         metadata_file=meta, dry_run=True)
        chapters = sum(1 for ln in meta.read_text().splitlines() if ln.startswith("[CHAPTER]"))
        meta.unlink(missing_ok=True)
        print(f"  dry run: {chapters} chapter markers, "
              f"{_human(sum(e[2] for e in entries))} total")
        return 0
    path = audio.encode_m4b(entries, out, book.title, book.author,
                            bitrate=args.bitrate or book.bundle.get("bitrate", "64k"))
    mb = path.stat().st_size / 1e6
    size = f"{mb:.1f} MB" if mb >= 0.1 else f"{path.stat().st_size / 1e3:.0f} KB"
    print(f"  {path}  {size}")
    return 0


def cmd_voices(args) -> int:
    if not shutil.which(VOCELLO):
        print("vocello not installed")
        return 1
    r = subprocess.run([VOCELLO, "voice", "list"], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    return 0


def cmd_voice_backup(args) -> int:
    src = Path.home() / "Library/Application Support/QwenVoice/voices"
    if not src.is_dir():
        print(f"no voices directory at {src}")
        return 1
    dest = Path(os.path.expanduser(args.dest)) if args.dest else config.voices_dir()
    dest.mkdir(parents=True, exist_ok=True)
    hits = list(src.glob(f"{args.name}*"))
    if not hits:
        print(f"no files matching {args.name!r}")
        return 1
    for f in hits:
        target = dest / f.name
        if f.is_dir():
            shutil.copytree(f, target, dirs_exist_ok=True)
        else:
            shutil.copy2(f, target)
        print(f"  {f.name}")
    print(f"backed up {len(hits)} items to {dest}")
    return 0


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge",
                               description="Reproducible audiobook rendering with vocello.")
    p.add_argument("-b", "--book", help="book slug or directory (default: the only book)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def book_arg(sp):
        # accept `forge render closer-encounters --section ...` as well as -b
        sp.add_argument("book", nargs="?", help="book slug or directory")
        return sp

    d = book_arg(sub.add_parser("doctor", help="check tools, voice, paths for this machine"))
    d.set_defaults(func=cmd_doctor)

    sub.add_parser("books", help="list books").set_defaults(func=cmd_books)

    n = sub.add_parser("new", help="create a book directory")
    n.add_argument("slug")
    n.add_argument("--source", help="path to epub/pdf/docx/txt")
    n.add_argument("--title")
    n.add_argument("--author")
    n.add_argument("--voice")
    n.add_argument("--out-dir", dest="out_dir", help="where rendered audio goes")
    n.add_argument("--force", action="store_true")
    n.set_defaults(func=cmd_new)

    e = book_arg(sub.add_parser("extract", help="build sections.json from the source book"))
    e.add_argument("--source")
    e.add_argument("--start-at", dest="start_at")
    e.add_argument("--end-at", dest="end_at")
    e.add_argument("--skip", action="append", help="regex; matching lines dropped")
    e.add_argument("--heading-pattern", dest="heading_pattern",
                   help="extra heading regex for this book, added to the built-ins")
    e.set_defaults(func=cmd_extract)

    s = book_arg(sub.add_parser("sections", help="list sections and estimated durations"))
    s.set_defaults(func=cmd_sections)

    r = book_arg(sub.add_parser("render", help="render sections to audio"))
    r.add_argument("--section", action="append", help="name or number; repeatable")
    r.add_argument("--voice")
    r.add_argument("--window", type=int)
    r.add_argument("--chunks", type=int, help="chunk size in characters")
    r.add_argument("--out")
    r.add_argument("--keep-takes", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-progress", dest="progress", action="store_false", default=True,
                   help="no bar, no progress lines, no work/progress.json")
    r.set_defaults(func=cmd_render)

    st = book_arg(sub.add_parser("status", help="what is rendered, what is pending"))
    st.set_defaults(func=cmd_status)

    w = book_arg(sub.add_parser("watch", help="follow a render started elsewhere"))
    w.add_argument("--section", action="append", help="name or number; repeatable")
    w.add_argument("-f", "--follow", action="store_true",
                   help="redraw until the run finishes")
    w.add_argument("--interval", type=float, default=2.0,
                   help="seconds between refreshes (default 2)")
    w.add_argument("--json", action="store_true", help="machine-readable snapshot")
    w.set_defaults(func=cmd_watch)

    v = book_arg(sub.add_parser("verify", help="duration, levels, optional transcript check"))
    v.add_argument("--section", action="append")
    v.add_argument("--transcribe", action="store_true")
    v.set_defaults(func=cmd_verify)

    b = book_arg(sub.add_parser("bundle", help="encode a chapterised .m4b from rendered sections"))
    b.add_argument("--out")
    b.add_argument("--bitrate")
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(func=cmd_bundle)

    sub.add_parser("voices", help="list vocello voices").set_defaults(func=cmd_voices)

    vb = sub.add_parser("voice-backup", help="copy a voice profile for another machine")
    vb.add_argument("name")
    vb.add_argument("--dest")
    vb.set_defaults(func=cmd_voice_backup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ex.ExtractError as exc:
        print(f"extract error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        missing = exc.filename or "a required tool"
        print(f"error: {missing} not found on PATH", file=sys.stderr)
        print("run `forge doctor` to see what is missing", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
