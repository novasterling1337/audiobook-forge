#!/bin/sh
# End-to-end check that needs no real book: builds a three-paragraph book in a
# temporary directory, renders it, verifies it, bundles a chapterised .m4b and
# cleans up. Run it after changing the renderer, or on a new machine.
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/smoke.txt" <<'TXT'
Smoke test one. This is the first paragraph, and it should be spoken in the cloned voice.

Smoke test two. This is the second paragraph. It proves joining, gaps and chaptering.

Smoke test three. The quick brown fox jumps over a very lazy dog while the narrator keeps talking.
TXT

FORGE_BOOKS="$tmp/books"
export FORGE_BOOKS
mkdir -p "$FORGE_BOOKS"

voice=${FORGE_SMOKE_VOICE:-"Jason Jorjani (icl exact)"}

"$repo/bin/forge" new smoke --source "$tmp/smoke.txt" --title "Smoke Test" \
  --author "Smoke" --voice "$voice" --out-dir "$tmp/out" >/dev/null

"$repo/bin/forge" extract smoke

render=$("$repo/bin/forge" render smoke)
echo "$render"
case "$render" in
  *"PROG "*) : ;;
  *) echo "FAIL: render printed no progress line over a pipe" >&2; exit 1 ;;
esac

snapshot="$FORGE_BOOKS/smoke/work/progress.json"
[ -s "$snapshot" ] || { echo "FAIL: no progress.json written" >&2; exit 1; }
python3 - "$snapshot" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["phase"] == "done", d["phase"]
assert d["clipsDone"] == d["clipsTotal"] > 0, d
assert d["percent"] == 100.0, d["percent"]
print(f"progress snapshot ok: {d['clipsDone']} clips, {d['audioSeconds']}s audio")
PY

"$repo/bin/forge" status smoke
"$repo/bin/forge" verify smoke

# watch must work with no live render at all: everything is finished here.
watch=$("$repo/bin/forge" watch smoke)
echo "$watch"
case "$watch" in
  *"1/1 rendered"*) : ;;
  *) echo "FAIL: watch did not report the finished section" >&2; exit 1 ;;
esac
"$repo/bin/forge" watch smoke --json | python3 -c '
import json, sys
d = json.load(sys.stdin)
s = d["sections"][0]
assert s["rendered"] and s["phase"] == "done", s
assert s["clipsDone"] == s["clipsTotal"] > 0, s
print("watch snapshot ok:", s["clipsDone"], "clips")
'

"$repo/bin/forge" bundle smoke

second=$("$repo/bin/forge" render smoke)
echo "$second"
case "$second" in
  *SKIP*) : ;;
  *) echo "FAIL: second render did not skip the finished section" >&2; exit 1 ;;
esac

m4b="$tmp/out/Smoke Test (unabridged).m4b"
[ -s "$m4b" ] || { echo "FAIL: no m4b written" >&2; exit 1; }
chapters=$(ffprobe -v error -show_chapters -of json "$m4b" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("chapters", [])))')
[ "$chapters" = "1" ] || { echo "FAIL: expected 1 chapter, got $chapters" >&2; exit 1; }

echo "smoke test passed"
