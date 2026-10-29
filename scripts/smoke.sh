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
"$repo/bin/forge" render smoke
"$repo/bin/forge" status smoke
"$repo/bin/forge" verify smoke
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
