"""Chunking: paragraphs -> clips small enough for a single vocello take."""
from __future__ import annotations

import re

SENTENCE_SPLIT = re.compile(r'(?<=[.!?\u2026\u201d"])\s+')


def split_sentences(paragraph: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(paragraph) if s]


def build_chunks(paragraphs: list[str], limit: int = 850) -> list[tuple[str, bool]]:
    """Return [(text, ends_paragraph)].

    Chunks are packed at sentence boundaries so a clip never ends mid-sentence
    (a clipped take restarts awkwardly). `ends_paragraph` tells the joiner
    whether to insert a paragraph gap or a smaller intra-paragraph gap.
    """
    out: list[tuple[str, bool]] = []
    for para in paragraphs:
        buf = ""
        for sentence in split_sentences(para):
            candidate = f"{buf} {sentence}".strip()
            if len(candidate) > limit and buf:
                out.append((buf, False))
                buf = sentence
            else:
                buf = candidate
        if buf:
            out.append((buf, True))
    return out


def plan(section: dict, limit: int = 850, wpm: float = 180.0) -> dict:
    paras = section["paras"]
    chunks = build_chunks(paras, limit)
    words = sum(len(p.split()) for p in paras)
    return {
        "chunks": chunks,
        "words": words,
        "expected_seconds": words / wpm * 60 if wpm else 0.0,
        "paragraphs": len(paras),
    }
