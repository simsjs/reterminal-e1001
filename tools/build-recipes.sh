#!/usr/bin/env bash
# Copies the shared screen header into every screen, then rebuilds the four
# recipe zips that LaraPaper imports.
#
#   shared/header.css     how the header looks (one copy for every screen)
#   shared/header.liquid  the header for the LaraPaper recipes
#
# It writes:
#   <recipe>/recipe/src/shared.liquid   between the BEGIN/END shared header lines
#   kitchen-board/app/templates/_header.css
#   <recipe>/<recipe>-recipe.zip
#
# Run it from anywhere:  tools/build-recipes.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RECIPES=(polestar dominion strava weather)

python3 - "$ROOT" "${RECIPES[@]}" <<'PY'
import pathlib, sys

root = pathlib.Path(sys.argv[1])
css = (root / "shared/header.css").read_text()
block = (root / "shared/header.liquid").read_text()
indented = "".join("  " + line if line.strip() else line for line in css.splitlines(True))
block = block.replace("/*HEADER_CSS*/\n", indented)

begin = "{% comment %} ==== BEGIN shared header: copied from shared/ by tools/build-recipes.sh. Edit it there, not here. ==== {% endcomment %}\n"
end = "{% comment %} ==== END shared header ==== {% endcomment %}\n"

for name in sys.argv[2:]:
    path = root / name / "recipe/src/shared.liquid"
    text = path.read_text()
    if begin not in text or end not in text:
        sys.exit(f"{path}: missing the BEGIN/END shared header lines")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    new = head + begin + block + end + tail
    if new != text:
        path.write_text(new)
        print(f"updated {path.relative_to(root)}")

kb = root / "kitchen-board/app/templates/_header.css"
note = "/* Copied from shared/header.css by tools/build-recipes.sh. Edit it there, not here. */\n"
if not kb.exists() or kb.read_text() != note + css:
    kb.write_text(note + css)
    print(f"updated {kb.relative_to(root)}")
PY

for r in "${RECIPES[@]}"; do
  zip_path="$ROOT/$r/$r-recipe.zip"
  rm -f "$zip_path"
  (cd "$ROOT/$r/recipe" && zip -qrX "$zip_path" src)
  echo "built $r/$r-recipe.zip"
done
