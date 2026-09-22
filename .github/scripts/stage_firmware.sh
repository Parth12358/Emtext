#!/usr/bin/env bash
# Build the tree that becomes the PUBLIC firmware repo (and the release source
# zip) from this private repo. One script, used by both the mirror and the
# release workflows, so what people can browse and what they download are the
# same bytes.
#
#   usage: .github/scripts/stage_firmware.sh <out-dir>
#
# What goes in:
#   emtext/              firmware/emtext (sketch + src), minus secrets.h and build/
#   docs/                the three firmware docs, links rewritten for the flat layout
#   README.md            firmware/mirror/README.md (the public landing page)
#   LICENSE              firmware/LICENSE, if present
#   .github/workflows/   firmware/mirror/.github/workflows (a compile check for PRs)
#
# What never goes in: anything under server/, eval/, tunnel/, website/, the
# root README/CLAUDE.md, TODO/RESEARCH, and secrets.h even if someone commits
# one by mistake (.gitignore already blocks it; this is the second lock).
set -euo pipefail

out="${1:?usage: stage_firmware.sh <out-dir>}"
root="$(git rev-parse --show-toplevel)"
rm -rf "$out"
mkdir -p "$out/emtext" "$out/docs" "$out/.github/workflows"

# --- sketch ---------------------------------------------------------------
# cp + explicit removals rather than rsync, so the script also runs in a bare
# Git Bash on Windows for local checks.
cp -a "$root/firmware/emtext/." "$out/emtext/"
rm -rf "$out/emtext/build"
find "$out/emtext" -name 'secrets.h' -delete
find "$out/emtext" -name '*.bak' -delete
if [ -e "$out/emtext/secrets.h" ]; then
  echo "refusing to stage: secrets.h present after exclude" >&2; exit 1
fi

# --- docs, flattened ------------------------------------------------------
cp "$root/docs/guides/firmware.md"                "$out/docs/firmware.md"
cp "$root/docs/design/firmware-ui-ux.md"          "$out/docs/firmware-ui-ux.md"
cp "$root/docs/design/firmware-requirements.md"   "$out/docs/firmware-requirements.md"

# The docs cross-link each other and the private repo's root files. In the
# mirror they sit side by side under docs/, and the private files do not
# exist, so: sibling links become bare filenames; links into the server repo
# become plain words. Keep this list in step with the docs' actual links.
for f in "$out"/docs/*.md; do
  sed -i \
    -e 's#\](\.\./design/firmware-ui-ux\.md#](firmware-ui-ux.md#g' \
    -e 's#\](\.\./design/firmware-requirements\.md#](firmware-requirements.md#g' \
    -e 's#\](\.\./guides/firmware\.md#](firmware.md#g' \
    -e 's#\](firmware-ui-ux\.md#](firmware-ui-ux.md#g' \
    -e 's#\](\.\./\.\./CLAUDE\.md)#(the emtext server repo, private)#g' \
    -e 's#\](\.\./\.\./README\.md)#(the emtext server repo, private)#g' \
    -e 's#`docs/design/firmware-ui-ux\.md`#`docs/firmware-ui-ux.md`#g' \
    -e 's#`docs/design/firmware-requirements\.md`#`docs/firmware-requirements.md`#g' \
    -e 's#`docs/guides/firmware\.md`#`docs/firmware.md`#g' \
    -e 's#`docs/status/TODO\.md`#the server repo'"'"'s TODO#g' \
    -e 's#`docs/design/RESEARCH\.md`#the server repo'"'"'s research notes#g' \
    -e 's#`server/static/index\.html`#the server'"'"'s browser client#g' \
    "$f"
done

# --- landing page, licence, public-repo CI --------------------------------
cp "$root/firmware/mirror/README.md" "$out/README.md"
[ -f "$root/firmware/LICENSE" ] && cp "$root/firmware/LICENSE" "$out/LICENSE"
cp "$root"/firmware/mirror/.github/workflows/*.yml "$out/.github/workflows/"

# Provenance, so a mirror commit or a zip can always be traced to its source.
{
  echo "source: Parth12358/Emtext"
  echo "commit: $(git -C "$root" rev-parse HEAD)"
  echo "date:   $(git -C "$root" log -1 --format=%cI HEAD)"
  echo "ref:    ${GITHUB_REF:-$(git -C "$root" rev-parse --abbrev-ref HEAD)}"
} > "$out/SOURCE.txt"

echo "staged $(find "$out" -type f | wc -l) files into $out"
