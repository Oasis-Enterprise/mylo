# Releasing Mylo

One command cuts a release. It exists because version + changelog used to
drift and tags landed before the bump/notes — the HA add-on update screen
and the in-app version tag both read from the **tagged commit**, so
everything must be committed before the tag.

## Steps

1. **Write the changelog entry first.** Add a `## [X.Y.Z] — YYYY-MM-DD`
   section at the top of `CHANGELOG.md` (Keep a Changelog format). The
   release script refuses to run without it — this is what HA shows on the
   add-on update screen.
2. **Run the release script** from a clean `main`:

   ```bash
   scripts/release.sh 1.3.2
   ```

   It bumps all three version locations (`config.yaml`,
   `pyproject.toml`, `src/mylo/__init__.py`), runs the CI gate
   (ruff + unit tests), commits `release: v1.3.2`, tags it with the
   changelog section as the annotation, and pushes `main` + the tag.
   Pushing the tag triggers the multi-arch image build
   (`.github/workflows/release.yml`).

   Preview without pushing: `DRY_RUN=1 scripts/release.sh 1.3.2`
   (commits + tags locally only).

3. **Create the GitHub release** from the tag once the build is green.

## Why all three version files

- `config.yaml` — the HA add-on manifest; drives the store's update prompt.
- `src/mylo/__init__.py` (`__version__`) — shown under the Mylo name in the
  UI (via `/api/status`).
- `pyproject.toml` — Python packaging metadata.

The script keeps them in lockstep so they can never disagree again.
