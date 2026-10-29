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
from collections import Counter
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


def to_text(src: Path, heading_pattern: str | None = None) -> str:
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
                    return reflow_pdf(r.stdout, heading_pattern)
        try:  # optional, better at columns
            import pymupdf  # type: ignore

            with pymupdf.open(src) as doc:
                pages = [page.get_text("blocks") for page in doc]
            flat = [[b[4] for b in page] for page in pages]
            return "\n\n".join(p.strip() for page in flat for p in page if p.strip())
        except Exception as exc:  # noqa: BLE001
            raise ExtractError(f"cannot read PDF {src.name}: {exc}") from exc

    raise ExtractError(f"unsupported format {suffix or src.name}")


TERMINAL_PUNCT = re.compile(r"[.!?\u2026\"\u201d\u2019)\]]$")
HYPHEN_BREAK = re.compile(r"[A-Za-z]-$")


def is_heading_line(line: str, heading_pattern: str | None = None,
                    *, at_page_top: bool = False) -> bool:
    """True if this single line is a section heading rather than body text.

    Chapter openers usually sit at the top of a page, but not always: in
    Sataneon Chapter 1 starts mid-page. Mid-page detection is therefore
    confined to the book's own `heading_pattern`, because the built-in rules
    would also promote table-of-contents entries and numbered footnotes into
    sections (Prometheus and Atlas gained four phantom sections that way).
    """
    if heading_pattern and _heading_regex(heading_pattern).match(line):
        return len(line) <= 110 and not line.rstrip().endswith(".")
    if not at_page_top:
        return False
    return split_heading(line) is not None


def reflow_pdf(text: str, heading_pattern: str | None = None) -> str:
    """Rebuild paragraphs from pdftotext output.

    pdftotext hard-wraps at the physical line width and emits almost no blank
    lines: a 117 page book came out as 4,040 lines with 128 blank-line
    separators for roughly 600 paragraphs. `to_blocks` splits only on blank
    lines, so without this pass a whole PDF collapses into a handful of blocks
    and sectionising yields one 40k-word `Front Matter`.

    Rebuild structure from line shape instead:

    * a form feed starts a page; a page whose first line is a heading opens a
      new section, because chapter openers land at page tops in print;
    * a line ending in terminal punctuation followed by a line starting with a
      capital opens a new paragraph;
    * a line ending in a hyphen rejoins with the next line without a space;
    * bare page numbers and running heads repeated across pages are dropped.

    Output is blank-line separated so the EPUB path (pandoc) and the PDF path
    feed `to_blocks` identically.
    """
    pages = text.replace("\r\n", "\n").replace("\r", "\n").split("\f")
    counts = Counter(
        line.strip() for page in pages for line in page.splitlines() if line.strip()
    )
    running_heads = {line for line, n in counts.items() if n >= 4}

    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current).strip())
            current.clear()

    for page in pages:
        lines = [line.strip() for line in page.splitlines()]
        lines = [
            line
            for line in lines
            if line
            and line not in running_heads
            and not PAGE_NUMBER_RE.fullmatch(line)
        ]
        for i, line in enumerate(lines):
            if is_heading_line(line, heading_pattern, at_page_top=(i == 0)):
                flush()
                paragraphs.append(line)
                continue
            if not current:
                current.append(line)
                continue
            prev = current[-1]
            if HYPHEN_BREAK.search(prev) and line[:1].islower():
                current[-1] = prev[:-1] + line
                continue
            starts_new = line[:1].isupper() or line[:1] in "\u201c\"'\u2018("
            if TERMINAL_PUNCT.search(prev) and starts_new:
                flush()
            current.append(line)
        # A paragraph continues across a page break; do not flush here.
    flush()
    return "\n\n".join(p for p in paragraphs if p)


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


def split_heading(block: str, heading_pattern: str | None = None) -> tuple[str, str] | None:
    """Return (name, title) if this block is a section heading.

    `heading_pattern` is a per-book regex that ADDS a heading form the built-in
    rules miss (`2. Catastrophic Information`); built-in headings still match.
    It is configuration, not a global default: plain numbered lines are list
    items in some books (Closer Encounters) and chapter headings in others
    (Lovers of Sophia).
    """
    if len(block) > 110:
        return None
    if heading_pattern:
        m = _heading_regex(heading_pattern).match(block)
        if m:
            groups = m.groupdict()
            if groups.get("name"):
                name = normalise_name(groups["name"].strip())
                return name, (groups.get("title") or "").strip()
            return _numbered_heading(block)
    m = HEADING_RE.match(block)
    if not m:
        return None
    name = m.group("name").strip()
    title = (m.group("title") or "").strip()
    name = normalise_name(name)
    return name, title


_HEADING_CACHE: dict[str, re.Pattern] = {}


def _heading_regex(pattern: str) -> re.Pattern:
    if pattern not in _HEADING_CACHE:
        _HEADING_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
    return _HEADING_CACHE[pattern]


def _numbered_heading(block: str) -> tuple[str, str]:
    """`2. Catastrophic Information` -> ('Chapter 2', 'Catastrophic Information')."""
    text = block.strip().rstrip(".")
    m = re.match(r"^(?P<num>\d{1,2})\s*[.):]\s*(?P<title>\S.*)$", text)
    if m:
        return f"Chapter {m.group('num')}", m.group("title").strip()
    return normalise_name(text), ""


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
               start_at: str | None = None, end_at: str | None = None,
               heading_pattern: str | None = None) -> list[dict]:
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

        head = split_heading(block, heading_pattern)
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
            heading_pattern: str | None = None, quiet: bool = False) -> list[dict]:
    if not src.exists():
        raise ExtractError(f"source not found: {src}")
    skip_res = [re.compile(p, re.IGNORECASE) for p in (skip_lines or [])]
    blocks = to_blocks(to_text(src, heading_pattern), skip_res)
    sections = sectionise(blocks, start_at=start_at, end_at=end_at,
                          heading_pattern=heading_pattern)
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
