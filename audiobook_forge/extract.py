"""Turn a book file into `sections.json`.

One JSON array of sections, in reading order:

    [{"no": 0, "name": "Introduction", "title": "", "paras": ["...", "..."]}]

`paras` are the paragraphs that will be spoken, one paragraph per entry, in
order. A chapter that carries a subtitle repeats it as `paras[0]`, so the
audio opens with the title naturally.

Supported inputs: EPUB / DOCX / HTML / Markdown / TXT (via pandoc or plain
read) and PDF (pdftotext, then pandoc, then pymupdf as fallbacks).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

# Section headings we recognise. Anything else stays body text, which is the
# safe failure mode: an unrecognised heading is read aloud rather than lost.
HEADING_RE = re.compile(
    r"^(?P<name>"
    r"chapter\s+[0-9ivxlc]+|part\s+[0-9ivxlc]+|book\s+[0-9ivxlc]+|"
    r"introduction|prologue|preface|foreword|conclusion|epilogue|afterword|"
    r"appendix(?:\s+[a-z0-9]+)?|works\s+cited|bibliography|references|"
    r"notes|index|glossary|acknowledgements|acknowledgments|"
    r"about\s+the\s+author"
    r")(?:\s*[:.\u2013\u2014-]\s*(?P<title>.*))?$",
    re.IGNORECASE,
)

PAGE_NUMBER_RE = re.compile(r"^[\divxlcdm]{1,6}$", re.IGNORECASE)
# Pandoc leaves footnote back-links as their own block ("[←1 ]", "[↑]"). They
# are navigation, not text, and would otherwise be spoken as "left arrow one".
ARTIFACT_RE = re.compile(r"^\[\s*[\u2190\u2191\u2192\u21a9]?\s*\d{0,4}\s*\]$")
# Superscript digits are footnote anchors left inline by the EPUB. Spoken
# aloud they become a stray number mid-sentence, so they are removed.
SUPERSCRIPT_DIGITS = re.compile("[\u00b9\u00b2\u00b3\u2070\u2074-\u2079]")


class ExtractError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# text acquisition
# --------------------------------------------------------------------------
def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def to_text(src: Path) -> str:
    """Return plain text for any supported book format."""
    suffix = src.suffix.lower()
    if suffix in (".txt", ".text"):
        return src.read_text(encoding="utf-8", errors="replace")

    if suffix in (".epub", ".docx", ".html", ".htm", ".md", ".markdown", ".rtf", ".odt"):
        if not shutil.which("pandoc"):
            raise ExtractError(f"pandoc is required to read {suffix} files")
        r = _run(["pandoc", str(src), "-t", "plain", "--wrap=none"])
        if r.returncode != 0 or not r.stdout.strip():
            raise ExtractError(f"pandoc failed on {src.name}: {r.stderr[-300:]}")
        return r.stdout

    if suffix == ".pdf":
        for cmd in (["pdftotext", "-layout", str(src), "-"], ["pdftotext", str(src), "-"]):
            if shutil.which(cmd[0]):
                r = _run(cmd)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout
        try:  # optional, better at columns
            import pymupdf  # type: ignore

            with pymupdf.open(src) as doc:
                return "\n\n".join(page.get_text() for page in doc)
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(f"cannot read PDF {src.name}: {exc}") from exc

    raise ExtractError(f"unsupported format {suffix or src.name}")


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------
def to_blocks(text: str, skip_res: list[re.Pattern] | None = None) -> list[str]:
    """Split into paragraphs, dropping page furniture."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    for raw in re.split(r"\n\s*\n", text):
        block = " ".join(line.strip() for line in raw.splitlines() if line.strip())
        block = SUPERSCRIPT_DIGITS.sub("", block)
        # Collapse every Unicode space separator to a plain space so chunking,
        # hashing and duration prediction are stable across sources.
        block = re.sub("[ \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+", " ", block).strip()
        if not block:
            continue
        if PAGE_NUMBER_RE.fullmatch(block):
            continue
        if ARTIFACT_RE.match(block):
            continue
        if any(p.search(block) for p in (skip_res or [])):
            continue
        blocks.append(block)
    return blocks


def split_heading(block: str) -> tuple[str, str] | None:
    """Return (name, title) if this block is a section heading."""
    if len(block) > 110:
        return None
    m = HEADING_RE.match(block)
    if not m:
        return None
    name = m.group("name").strip()
    title = (m.group("title") or "").strip()
    name = normalise_name(name)
    return name, title


def normalise_name(name: str) -> str:
    if name.isupper():
        name = name.title()
    if re.match(r"^(chapter|part|book)\s+", name, re.IGNORECASE):
        word, num = name.split(None, 1)
        word = word.capitalize()
        num = num.upper() if re.fullmatch(r"[ivxlc]+", num, re.IGNORECASE) else num
        return f"{word} {num}"
    # collapse MULTIPLE   SPACES, keep original casing otherwise
    return re.sub(r"\s+", " ", name)


def looks_like_subtitle(block: str) -> bool:
    if len(block) > 120:
        return False
    if block.endswith((".", "!", "?", "…")):
        return False
    if "://" in block or "@" in block:
        return False
    words = block.split()
    if not 1 <= len(words) <= 14:
        return False
    # a body sentence that merely ends without punctuation is unlikely to be
    # this short; requiring capitals keeps "and then the thing" out
    return block[:1].isupper()


# --------------------------------------------------------------------------
# section assembly
# --------------------------------------------------------------------------
def sectionise(blocks: list[str], cfg: dict | None = None,
               start_at: str | None = None, end_at: str | None = None) -> list[dict]:
    """Group blocks into sections. `start_at`/`end_at` are regexes."""
    cfg = cfg or {}
    start_re = re.compile(start_at) if start_at else None
    end_re = re.compile(end_at) if end_at else None

    sections: list[dict] = []
    current = {"name": "Front Matter", "title": "", "paras": []}
    started = start_re is None

    for block in blocks:
        if end_re and end_re.search(block):
            break
        if not started:
            if start_re and start_re.search(block):
                started = True
            else:
                continue

        head = split_heading(block)
        if head:
            name, title = head
            if current["paras"] or current["title"]:
                sections.append(current)
            current = {"name": name, "title": title, "paras": []}
            continue

        if not current["paras"] and not current["title"] and looks_like_subtitle(block):
            current["title"] = block
            current["paras"].append(block)
            continue

        current["paras"].append(block)

    if current["paras"] or current["title"]:
        sections.append(current)

    sections = [s for s in sections if s["paras"]]
    for i, sec in enumerate(sections):
        sec["no"] = i
    return sections


def extract(src: Path, out: Path, *, start_at: str | None = None,
            end_at: str | None = None, skip_lines: list[str] | None = None,
            quiet: bool = False) -> list[dict]:
    if not src.exists():
        raise ExtractError(f"source not found: {src}")
    skip_res = [re.compile(p, re.IGNORECASE) for p in (skip_lines or [])]
    blocks = to_blocks(to_text(src), skip_res)
    sections = sectionise(blocks, start_at=start_at, end_at=end_at)
    if not sections:
        raise ExtractError("no sections detected; check start_at / heading rules")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(sections, ensure_ascii=False, indent=1), encoding="utf-8")
    if not quiet:
        for s in sections:
            words = sum(len(p.split()) for p in s["paras"])
            print(f"  {s['no']:02d}  {s['name']:<22} {len(s['paras']):>4} paras "
                  f"{words:>7} words  {seconds(words)/60:6.1f} min")
    return sections


def seconds(words: int, wpm: float = 180.0) -> float:
    return words / wpm * 60
