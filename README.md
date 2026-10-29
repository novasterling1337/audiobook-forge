# audiobook-forge

Chapter-wise audiobook rendering with vocello voice cloning, built to be
resumable, verifiable and portable between machines.

One book is one directory. The pipeline is four commands:

```
forge new <slug> --source book.epub     # scaffold ~/.config/audiobooks/books/<slug>/book.toml
forge extract <slug>                    # EPUB/PDF/DOCX/TXT -> sections.json
forge render <slug>                     # sections.json -> out_dir/NN Name.wav
forge bundle <slug>                     # optionally: one chapterised .m4b
```

Nothing here is specific to a single book: source format, section detection,
voice, pacing and where the audio lands all come from `book.toml`.

## Where things live

The checkout is code only. Everything per-machine is under
`~/.config/audiobooks`, so a book definition never sits next to source, needs no
`.gitignore` entry and needs no separate backup step:

```
~/.config/audiobooks/
├── books/<slug>/
│   ├── book.toml              everything book-specific
│   ├── sections.json          derived text; expensive to reproduce
│   └── work/                  takes + state.json while a section renders
└── voices/<name>.*            exported vocello profiles (forge voice-backup)
```

`forge books` lists what is there. `FORGE_CONFIG_ROOT` moves the whole tree,
`FORGE_BOOKS` moves just the books directory. Rendered audio is separate again:
wherever `out_dir` points, default `~/Audiobooks/<slug>`.

## Requirements

| Tool | Why | Install |
|---|---|---|
| Python 3.11+ | the CLI (stdlib only, no pip install needed) | system or pyenv |
| `vocello` | speech synthesis | vocello's own installer |
| `pandoc` | EPUB / DOCX / HTML / ODT input | `brew install pandoc` |
| `ffmpeg`, `ffprobe` | gaps, joining, m4b encoding | `brew install ffmpeg` |
| `pdftotext` | PDF input (optional) | `brew install poppler` |
| `whisper-cli` | optional transcript spot-checks | `brew install whisper-cpp` |

`forge doctor <slug>` reports all of the above plus voice availability, output
paths, section count and free disk, and is the first thing to run on a new
machine.

The CLI needs no install: `bin/forge` is a shim that sets `PYTHONPATH` and runs
the package. Symlink it once:

```
ln -sf ~/Source/Audio/audiobook-forge/bin/forge ~/.local/bin/forge
```

## Layout

```
audiobook-forge/
├── bin/forge                  launcher (no install, no dependencies)
├── audiobook_forge/
│   ├── cli.py                 argparse front end
│   ├── config.py              defaults + book.toml + path resolution
│   ├── extract.py             any book file -> sections.json
│   ├── chunk.py               paragraphs -> clips under the single-take limit
│   ├── render.py              windows, resume, joining, manifests
│   ├── verify.py              duration / level / transcript checks
│   └── audio.py               ffmpeg and ffprobe helpers
└── scripts/smoke.sh           end-to-end check without a real book
```

No `books/` directory in the checkout: book definitions are configuration, and
they live in `~/.config/audiobooks` (see above).

Rendered audio stays where `out_dir` points (default `~/Audiobooks/<slug>`).
For an existing book whose audio is elsewhere, set `out_dir` and the renderer
resumes instead of starting over.

## How a section gets made

1. **Chunk.** Paragraphs are packed at sentence boundaries into clips under
   `chunk_chars` (default 850, under vocello's 900-character single-take limit).
   Each clip records whether it ended a paragraph, which decides whether a
   0.45 s or 0.20 s gap follows it.
2. **Render in windows.** `window` clips (default 35) go to one `vocello batch`
   process. Large single batches abort with `batch_incomplete` and no useful
   error, so a window is a unit of failure, not the whole chapter. Missing rows
   are retried up to `attempts` times in a fresh process.
3. **Resume at clip granularity.** Every finished take is moved to
   `work/secNN/take_00042.wav` and recorded in `state.json` with a SHA-1 of its
   text. A crash costs the unfinished rows. Editing one paragraph re-renders
   one clip. A changed `chunk_chars` changes the plan and invalidates the whole
   section, which is correct: the clips no longer line up.
4. **Join and check.** Clips are concatenated in plan order with the gap files,
   and the real duration is compared with `words / wpm * 60`. Drift beyond
   `tolerance` (default 25%) is logged as `WARN`, and clips absurdly short for
   their word count are listed by index. Both are printed, never silently
   passed.

A `.json` manifest next to each `.wav` records words, chunks, durations, drift,
wall time, per-clip durations and hashes, so a later session can tell exactly
what produced a file.

## Commands

| Command | Purpose |
|---|---|
| `forge doctor [book]` | tools, voices, paths, section count, disk |
| `forge books` | list books and progress |
| `forge new <slug> --source ... [--title --author --voice]` | scaffold `~/.config/audiobooks/books/<slug>/book.toml` |
| `forge extract <slug> [--source --start-at --end-at --skip REGEX]` | rebuild `sections.json` |
| `forge sections <slug>` | sections, word counts, estimated minutes |
| `forge render <slug> [--section NAME\|N]... [--dry-run] [--window N] [--chunks N] [--voice V] [--out DIR] [--keep-takes]` | render |
| `forge status <slug>` | done / pending / excluded, per section |
| `forge verify <slug> [--section ...] [--transcribe]` | duration, drift, levels, optional transcript |
| `forge bundle <slug> [--out PATH --bitrate 64k --dry-run]` | chapterised `.m4b` |
| `forge voices` | vocello voice list |
| `forge voice-backup <name> [--dest DIR]` | copy a voice profile to `~/.config/audiobooks/voices/` |

Book selection accepts a slug, a directory, or a `book.toml` path, either as a
positional (`forge render closer-encounters`) or with `-b`.

Long renders are meant to run detached:

```
forge render <slug> --section "Chapter 5"
```

under whatever supervisor you use (Hermes background job, `caffeinate`,
`tmux`). The renderer is safe to kill at any point and safe to run twice.

## book.toml

Every key overrides `DEFAULTS` in `audiobook_forge/config.py`, so a minimal
book is four lines:

```toml
slug = "closer-encounters"
title = "Closer Encounters"
author = "Jason Reza Jorjani"
source = "~/Documents/E-books/Closer Encounters.epub"

out_dir = "~/Downloads/Closer Encounters (audio)"

[voice]
name = "Jason Jorjani (icl exact)"

[extract]
start_at = "^Introduction$"     # drop title page, copyright, praise
exclude = ["Works Cited", "Notes"]
skip_lines = ["OceanofPDF"]
```

Tuning that matters:

- `wpm` — measured pace of the voice. It only predicts duration and decides
  whether an existing file is plausible. Measure it once per voice: render one
  paragraph, divide words by minutes. 180 is a slow-ish narration pace.
- `chunk_chars` — bigger means fewer clips and fewer joins, but must stay under
  the engine's single-take limit.
- `window` — smaller survives flakier engines at some startup cost.
- `paragraph_gap`, `inner_gap` — 0.45 s / 0.20 s sounds like prose; larger
  sounds like a lecture.
- `tolerance` — keep it loose enough that normal pacing variance does not
  trigger REDO on every run.

## Section detection

`extract` recognises a heading line when it is short and starts with
Chapter/Part/Book + number, Introduction, Prologue, Preface, Foreword,
Conclusion, Epilogue, Afterword, Appendix, Works Cited, Bibliography,
References, Notes, Index, Glossary, Acknowledgements, or About the Author —
optionally followed by `:`, `.`, `-` or `—` and a subtitle.

A short, capitalised, unpunctuated paragraph directly after a heading becomes
the chapter subtitle and is repeated as the first spoken paragraph, so the
audio opens with "Chapter 4. Machinations in the Matrix of Time" naturally.

Text cleaning, applied to every block:

- Unicode space separators (`U+2005` around em-dashes is common in EPUBs)
  collapse to a plain space, so chunking and hashes are stable across sources.
- Footnote superscripts (`¹²³`, `U+2070-2079`) are removed; spoken aloud they
  become a stray number mid-sentence.
- Footnote back-links (`[←1 ]`, `[↑]`) are dropped.
- Stand-alone page numbers and any `skip_lines` regex are dropped.

If a book's headings are not recognised, the failure is visible and safe:
unrecognised headings stay in the body text and are read aloud, and
`forge sections` shows a `Front Matter` section holding everything that came
before the first real heading.

## Verification

`forge verify` checks duration against the word count, then peak and mean
level. The pass condition is percentage drift or 8 seconds of absolute slack,
because a 20 second section can drift 25 percent on clip padding alone while an
hour-long chapter should be judged by percentage. `--transcribe` spot-checks the
first and last 25 seconds with `whisper-cli`, which is how a truncated opening
or a chapter that ends mid-sentence gets caught.

Whisper is deliberately not the primary check: on an hour of audio it can skip
a paragraph and still produce plausible text, so duration is the quantitative
gate and transcripts are for spot checks.

`scripts/smoke.sh` exercises extract → render → verify → bundle on a throwaway
book and asserts the second render skips.

## Moving to another machine

The repo is self-contained: no third-party Python packages, no absolute paths
outside `book.toml`. On a new host:

1. `forge doctor` — install whatever it reports missing (`vocello`, `pandoc`,
   `ffmpeg`, optionally `whisper-cpp`), Python 3.11+.
2. Copy `~/.config/audiobooks` across. That single directory holds every book
   definition, every `sections.json` and every exported voice profile, so the
   migration is one `rsync`:

   ```
   rsync -a ~/.config/audiobooks/ newhost:~/.config/audiobooks/
   ```
3. Re-import the voice on the new machine: copy `voices/<name>.*` into
   `~/Library/Application Support/QwenVoice/voices/` (`.wav`, `.txt`,
   `.voice.json`, `.clone_prompt/`), then `forge voices` to confirm the profile
   is listed.
4. Point `out_dir` at the new audio location and run `forge render` — finished
   sections are skipped by duration check, so a partially rendered book
   continues instead of restarting.

`FORGE_CONFIG_ROOT` relocates the whole config tree (default
`~/.config/audiobooks`), `FORGE_BOOKS` just the books directory,
`FORGE_VOCELLO`, `FORGE_FFMPEG`, `FORGE_FFPROBE` and `FORGE_PYTHON` override
binaries, and
`FORGE_WHISPER_MODEL` points at the ggml model for transcript checks.

### Multiple machines

`book.toml` carries absolute paths, so each host keeps its own book definitions
and they are not interchangeable. A typical layout keeps sources and finished
audio outside the checkout:

| | path |
|---|---|
| source books | `~/Books/<author>/` |
| rendered audio | `~/Audiobooks/<author>/<book>/` |
| book + voice config | `~/.config/audiobooks/` |

Because `book.toml` carries those paths, each host has its own copy and they are
not interchangeable. `bin/forge` picks the first Python 3.11+ it finds
(`~/.local/bin`, Homebrew, `/usr/local`, then `PATH`) and honours
`FORGE_PYTHON`, because a stock macOS `python3` is 3.9 and `tomllib` needs 3.11.

## Known failure modes

| Symptom | Cause | Handling |
|---|---|---|
| `batch_incomplete` mid-chapter | engine aborts large batches, no documented limit | windows of 35 + retry of missing rows |
| one clip is much shorter than its text | engine dropped or truncated a take | `shortClips` in the manifest and a `WARN` line |
| duration far from prediction | duplicated or missing text, or a changed `chunk_chars` | `WARN` with drift percentage, file still written |
| a chapter reads its own title twice | source repeated the title as both heading and first paragraph | edit `sections.json`, delete the duplicate |
| stray numbers mid-sentence | inline footnote superscripts not stripped | already handled; add a `skip_lines` regex for other furniture |
