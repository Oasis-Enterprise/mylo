#!/usr/bin/env bash
# Cut a release in one shot, in the right order so nothing drifts.
#
#   scripts/release.sh <version>      e.g. scripts/release.sh 1.3.2
#
# What it does (and why each step matters):
#   1. Refuses unless you're on main with a clean tree (the release commit
#      must contain ONLY the version + changelog bump).
#   2. Requires a CHANGELOG.md entry for <version> FIRST — the HA add-on
#      update screen reads CHANGELOG.md from the tagged commit, so the
#      notes have to exist before we tag. Write them, then run this.
#   3. Bumps all three version locations together (config.yaml,
#      pyproject.toml, src/mylo/__init__.py) — the in-app "V" tag reads
#      __version__, the add-on store reads config.yaml, packaging reads
#      pyproject. They must match.
#   4. Runs the same gate as CI (ruff + unit tests). Aborts on failure.
#   5. Commits, then annotates the tag with the changelog section, then
#      pushes main + tag together — so the tagged commit (what the build
#      ships) always has the bump AND the changelog. Pushing the tag is
#      what triggers the image build (.github/workflows/release.yml).
#
# DRY_RUN=1 scripts/release.sh <version>  → do everything except push.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "release: $*" >&2; exit 1; }

VERSION="${1:-}"
[[ -n "$VERSION" ]] || die "usage: scripts/release.sh <version>  (e.g. 1.3.2)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z, got '$VERSION'"

TAG="v$VERSION"

# ── Preconditions ───────────────────────────────────────────────────────────
[[ -d .venv ]] || die ".venv not found — create it first (see scripts/dev.sh)"

branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || die "must be on main (on '$branch')"
[[ -z "$(git status --porcelain)" ]] || die "working tree not clean — commit or stash first"
git rev-parse "$TAG" >/dev/null 2>&1 && die "tag $TAG already exists"

# Changelog entry must already be written for this version.
grep -qE "^## \[$VERSION\]" CHANGELOG.md \
  || die "no '## [$VERSION]' section in CHANGELOG.md — add the release notes first, then re-run"

# ── Bump all three version locations ────────────────────────────────────────
# Each bump targets only the first matching line (flag set on a successful
# match, not per-line) so a stray 'version =' elsewhere can't be hit.
perl -i -pe "if (!\$seen && s/^version: \".*\"/version: \"$VERSION\"/) {\$seen=1}" config.yaml
perl -i -pe "if (!\$seen && s/^version = \".*\"/version = \"$VERSION\"/) {\$seen=1}" pyproject.toml
perl -i -pe "s/^__version__ = \".*\"/__version__ = \"$VERSION\"/" src/mylo/__init__.py

# Verify all three landed.
grep -qE "^version: \"$VERSION\"" config.yaml || die "config.yaml bump failed"
grep -qE "^version = \"$VERSION\"" pyproject.toml || die "pyproject.toml bump failed"
grep -qE "^__version__ = \"$VERSION\"" src/mylo/__init__.py || die "__init__.py bump failed"

# ── CI gate (mirror .github/workflows/ci.yml exactly) ───────────────────────
echo "release: running ruff + mypy + unit tests…"
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
.venv/bin/python -m pytest tests/unit -q

# ── Commit + annotated tag (notes from the changelog section) ────────────────
NOTES="$(awk -v v="## [$VERSION]" '
  index($0, v)==1 {grab=1; print; next}
  grab && /^## \[/ {exit}
  grab {print}
' CHANGELOG.md)"

git add config.yaml pyproject.toml src/mylo/__init__.py
git commit -q -m "release: $TAG"
git tag -a "$TAG" -m "$NOTES"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "release: DRY_RUN — committed and tagged $TAG locally, not pushed."
  echo "         push with: git push origin main $TAG"
  exit 0
fi

git push origin main "$TAG"
echo "release: $TAG pushed. The add-on image build is now running (Actions → Release)."
echo "         Create the GitHub release from $TAG when the build is green."
