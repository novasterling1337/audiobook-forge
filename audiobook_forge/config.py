"""Book configuration: built-in defaults, per-book TOML, CLI overrides.

A book is a directory containing `book.toml`, `sections.json` (produced by
`forge extract`), a `work/` scratch area and rendered audio in `out_dir`.
Nothing is hardcoded to one book, so a second audiobook is a new directory
plus a `forge extract`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

DEFAULTS: dict = {
    "render": {
        # Measured pace of the voice clone. Only used to predict duration and
        # to decide whether an existing file needs re-rendering.
        "wpm": 180.0,
        # Chunk ceiling in characters. Vocello's single-take mode refuses
        # anything over 900; stay under it with room for multi-byte characters.
        "chunk_chars": 850,
        # Clips per `vocello batch` process. Large batches abort with
        # `batch_incomplete` (no documented row limit, observed at 103/140),
        # so render in windows and retry only what is missing.
        "window": 35,
        "attempts": 3,
        "paragraph_gap": 0.45,
        "inner_gap": 0.20,
        # Accepted drift between audio duration and the wpm prediction.
        "tolerance": 0.25,
        # Keep intermediate takes after a section is joined. Useful for
        # re-cutting a single paragraph, expensive in disk.
        "keep_takes": False,
    },
    "voice": {"name": "", "mode": "clone"},
    "extract": {
        "start_at": None,      # regex; text before the first match is dropped
        "end_at": None,        # regex; text from the first match onward is dropped
        "skip_lines": [],      # regexes; matching lines are deleted outright
        "exclude": [],         # section names never rendered (Notes, Index, ...)
        # Extra heading form for this book only, e.g. "^\\d{1,2}\\.\\s+\\S" for
        # chapters titled "2. Catastrophic Information". Never a global default:
        # a numbered line is a list item in one book and a heading in another.
        "heading_pattern": None,
        # Ordered real titles, one per section, in reading order. Names a
        # section the heading rules could only read as `Chapter I` and peels a
        # title that the source glued onto the opening sentence.
        "titles": [],
        # TTS-only pronunciation rewrites, `{"Aeschylus": "ESS-kih-luss"}`.
        # Applied to the text stored in sections.json, never to the source.
        "respell": {},
    },
    "bundle": {
        "enabled": True,
        "bitrate": "64k",
        # Artwork for the .m4b. Absolute, or relative to the book directory.
        # When empty, cover.jpg / cover.jpeg / cover.png in the book dir is used.
        "cover": "",
    },
}


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Book:
    path: Path
    data: dict
    render: dict = field(default_factory=dict)
    voice: dict = field(default_factory=dict)
    extract: dict = field(default_factory=dict)
    bundle: dict = field(default_factory=dict)

    # identity -----------------------------------------------------------
    @property
    def slug(self) -> str:
        return self.data.get("slug") or self.path.name

    @property
    def title(self) -> str:
        return self.data.get("title") or self.slug.replace("-", " ").title()

    @property
    def author(self) -> str:
        return self.data.get("author") or ""

    @property
    def source(self) -> str | None:
        return self.data.get("source")

    # paths --------------------------------------------------------------
    @property
    def out_dir(self) -> Path:
        raw = self.data.get("out_dir") or f"~/Audiobooks/{self.slug}"
        return Path(os.path.expandvars(os.path.expanduser(raw)))

    @property
    def work_dir(self) -> Path:
        raw = self.data.get("work_dir") or str(self.path / "work")
        return Path(os.path.expandvars(os.path.expanduser(raw)))

    @property
    def sections_file(self) -> Path:
        raw = self.data.get("sections_file") or "sections.json"
        p = Path(os.path.expanduser(raw))
        return p if p.is_absolute() else self.path / p

    @property
    def log_file(self) -> Path:
        return self.work_dir / "render.log"

    # data ---------------------------------------------------------------
    def load_sections(self) -> list[dict]:
        import json

        if not self.sections_file.exists():
            raise ConfigError(
                f"no sections at {self.sections_file}; run `forge extract {self.slug}` first"
            )
        return json.loads(self.sections_file.read_text(encoding="utf-8"))

    def body_sections(self) -> list[dict]:
        """Sections that should be rendered, in book order."""
        excluded = {s.lower() for s in self.extract.get("exclude", [])}
        out = []
        for sec in self.load_sections():
            name = sec["name"]
            if name.lower() in excluded:
                continue
            if any(name.lower().startswith(e) for e in excluded):
                continue
            out.append(sec)
        return out

    def describe(self) -> str:
        return f"{self.title}" + (f" — {self.author}" if self.author else "")


def config_root() -> Path:
    """Where all per-machine state lives: book definitions and voice exports.

    Nothing book-specific belongs in the checkout. A book holds absolute source
    paths and a machine-specific output location, so it is configuration, not
    source: ~/.config/audiobooks by default.
    """
    raw = os.environ.get("FORGE_CONFIG_ROOT")
    return Path(os.path.expanduser(raw or "~/.config/audiobooks"))


def books_root() -> Path:
    raw = os.environ.get("FORGE_BOOKS")
    if raw:
        return Path(os.path.expanduser(raw))
    return config_root() / "books"


def voices_dir() -> Path:
    return config_root() / "voices"


def load(target: str | None = None) -> Book:
    """Resolve a book from a slug, a directory, or a book.toml path."""
    root = books_root()
    cand: Path
    if target is None:
        if not root.is_dir():
            raise ConfigError(f"no books directory at {root}")
        dirs = [d for d in root.iterdir() if (d / "book.toml").exists()]
        if len(dirs) != 1:
            names = ", ".join(sorted(d.name for d in dirs)) or "none"
            raise ConfigError(f"specify a book; available: {names}")
        cand = dirs[0]
    else:
        p = Path(os.path.expanduser(target))
        if p.is_file() and p.name == "book.toml":
            cand = p.parent
        elif (p / "book.toml").exists():
            cand = p
        elif (root / target / "book.toml").exists():
            cand = root / target
        else:
            raise ConfigError(f"no book.toml for {target!r}")

    data = tomllib.loads((cand / "book.toml").read_text(encoding="utf-8"))
    merged = _deep_merge(DEFAULTS, data)
    return Book(
        path=cand,
        data=merged,
        render=merged["render"],
        voice=merged["voice"],
        extract=merged["extract"],
        bundle=merged.get("bundle", {}),
    )
