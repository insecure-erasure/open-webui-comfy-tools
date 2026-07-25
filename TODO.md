# TODO: Smart Generate Image v4.0 — Decouple Workflows

Remove embedded JSON from Python scripts. Load workflows from Open WebUI's
`cache/tools/<tool_id>/` directory at runtime, with a bootstrap fallback.

## Motivation

Currently each tool script carries a ~2–8 KB raw JSON workflow string.
Updating the workflow requires editing the script, even though the `.py` file
is pasted into Open WebUI's Workspace → Tools UI as a blob.  By reading the
workflow from `cache/tools/<tool_id>/workflow.json` instead, admins can:

- Swap or tweak workflows without touching the tool source code
- Keep workflows under version control in `workflows/` independently
- Let each tool manage its own working directory (the `cache/tools/<id>/`
  dir is created automatically by Open WebUI when the tool is installed)

---

## Background

Open WebUI creates an empty directory at `CACHE_DIR / 'tools' / <tool_id>`
(`/app/backend/data/cache/tools/<tool_id>/`) every time a tool is created via
`POST /api/v1/tools/create`.  This directory is **never written to or read
from** by the framework itself — it is reserved for the tool's own use.

Since tools run as Python code **inside** the backend process, they have
direct filesystem access to this directory via `open()`, `Path.read_text()`,
etc.  Additionally, the `GET /cache/{path}` endpoint serves files from
`CACHE_DIR` with authentication (`get_verified_user`), so the JSON could
also be fetched via HTTP if needed.

---

## Milestones

### Milestone 1 — Bootstrap workflow to cache on first run

Each tool writes its embedded workflow to `cache/tools/<id>/workflow.json`
on its first invocation.  Subsequent runs load from disk.

- [ ] Add a `_BOOTSTRAP_WORKFLOW` constant (the embedded JSON) as fallback
- [ ] Add `_get_cache_dir()` helper that resolves the tool's cache directory
      at runtime (e.g. from `__id__` or a well-known path)
- [ ] Add `_load_workflow()` that:
      1. Checks `cache/tools/<tool_id>/workflow.json` on disk
      2. If missing → writes the embedded JSON there (bootstrap)
      3. Reads and returns the workflow from disk
- [ ] Apply to `smart_generate_image.py`
- [ ] Apply to `enhance_image.py`
- [ ] Apply to `generate_video.py`

### Milestone 2 — Remove embedded workflow (optional per tool)

Once bootstrapping works reliably, the embedded JSON can be dropped,
leaving only a minimal fallback or an error if the file is missing.

- [ ] Remove `_ENHANCE_WORKFLOW_JSON_RAW` from `enhance_image.py`
- [ ] Remove `_VIDEO_WORKFLOW_JSON_RAW` from `generate_video.py`
- [ ] Remove `_ZIT_WORKFLOW_JSON_RAW` from `smart_generate_image.py`
- [ ] Update `_load_workflow()` to raise / log a clear error if the file
      is not found and there is no embedded fallback

### Milestone 3 — Keep `workflows/` as the source of truth for Git

The `workflows/` directory stays in the repo.  It holds the canonical JSON
files that should be copied into the container's `cache/tools/<id>/`.

- [ ] Document the sync procedure in README
- [ ] Optionally add a `Makefile` or script target that copies
      `workflows/<tool>.json` into the correct cache paths inside the
      container

### Milestone 4 — Update README

- [ ] Add a section explaining the workflow loading strategy
- [ ] Document how to update a workflow in production (copy JSON → cache dir)
- [ ] Update the Workflows table to reflect the new purpose of `workflows/`
- [ ] Mention the `cache/tools/<id>/` directory and its HTTP endpoint
      (`GET /cache/tools/<id>/workflow.json`)

### Milestone 5 — Cleanup and verification

- [ ] Verify that `workflows/` JSON files are still in sync with the
      embedded fallbacks (if any remain)
- [ ] Verify that changing `workflows/enhance_image.json` and copying it
      to the cache dir takes effect without editing the tool code
- [ ] Verify the same for `generate_video` and `smart_generate_image`
- [ ] Verify that `GET /cache/tools/<tool_id>/workflow.json` returns the
      file with proper auth

---

## Notes

- The `cache/tools/<id>/` directory survives container restarts because it
  lives under `DATA_DIR` (typically a Docker volume mounted at
  `/app/backend/data/`).
- When a tool is updated via `POST /api/v1/tools/id/<id>/update`, Open WebUI
  does **not** recreate the cache directory — it already exists.
- Bootstrap on first run ensures zero-config deployment: paste the tool,
  use it, and the workflow JSON appears in the cache automatically.
