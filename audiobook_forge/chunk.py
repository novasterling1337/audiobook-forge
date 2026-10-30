"""Chunking: paragraphs -> clips small enough for a single vocello take."""
from __future__ import annotations

import re

SENTENCE_SPLIT = re.compile(r'(?<=[.!?\u2026\u201d"])\s+')
# Clause boundaries, tried in this order, before falling back to a word split.
CLAUSE_SPLITS = (r'(?<=[;:])\s+', r'(?<=[,])\s+', r'(?<=\u2014)\s+')


def split_sentences(paragraph: str) -> list[str]:
    return [s for s in SENTENCE_SPLIT.split(paragraph) if s]


def split_long(sentence: str, limit: int = 850) -> list[str]:
    """Cut one over-long sentence into pieces no longer than `limit`.

    A single sentence can run past vocello's ~900-character take limit even
    when the clip budget is 850, and then the whole window fails with "text is
    too long for one take". Break at the clause boundary nearest the middle
    first so the audio pauses where a reader would, and only fall back to a
    word split when the sentence has no usable clause boundary.
    """
    if len(sentence) <= limit:
        return [sentence]
    for pattern in CLAUSE_SPLITS:
        parts = [p for p in re.split(pattern, sentence) if p]
        if len(parts) > 1:
            out: list[str] = []
            for part in parts:
                out.extend(split_long(part, limit))
            return out
    # no clause boundary, or splitting on it did not shorten anything
    words = sentence.split()
    out, buf = [], ""
    for word in words:
        candidate = f"{buf} {word}".strip()
        if len(candidate) > limit and buf:
            out.append(buf)
            buf = word
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def build_chunks(paragraphs: list[str], limit: int = 850) -> list[tuple[str, bool]]:
    """Return [(text, ends_paragraph)].

    Chunks are packed at sentence boundaries so a clip never ends mid-sentence
    (a clipped take restarts awkwardly). `ends_paragraph` tells the joiner
    whether to insert a paragraph gap or a smaller intra-paragraph gap.
    """
    out: list[tuple[str, bool]] = []
    for para in paragraphs:
        buf = ""
        sentences = []
        for sentence in split_sentences(para):
            sentences.extend(split_long(sentence, limit))
        for sentence in sentences:
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
