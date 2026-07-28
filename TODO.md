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

## Repository state

- **Branch:** `feat/decouple-workflows`
- **Root:** `/srv/pi/smart_generate_image/`
- **Workflows directory:** `workflows/` — contains canonical JSONs with unique
  `_meta.title` values for nodes referenced by the tools
- **No embedded JSONs** in any Python script
- **No placeholders** (`{{PROMPT}}`, `{{SEED}}`) in any workflow JSON
- **Node resolution:** by `_meta.title` via `_resolve_node()` helper
- **Workflow loading:** from `CACHE_DIR / 'tools' / <__id__> / <script_name>.json`
- All three tools have been verified working in production

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

## Milestones

### ✅ Milestone 1 — Bootstrap workflow to cache on first run (completed)

Each tool writes its embedded workflow to `cache/tools/<id>/<script_name>.json`
on its first invocation.  Subsequent runs load from disk.

**Actual implementation:** `_load_workflow(tool_id, filename)` — reads from
`CACHE_DIR / 'tools' / <__id__> / <filename>`.  Bootstrap was removed in
Milestone 2; now it raises a clear error if the file is missing.

#### File naming convention

| Location | Filename |
|---|---|
| `workflows/` in Git | `enhance_image.json`, `generate_video.json`, `smart_generate_image.json` |
| `cache/tools/<id>/` at runtime | Same filename as in `workflows/` |

#### Key: how to get the tool's ID at runtime

Open WebUI injects `__id__` as a hidden parameter **into each tool function
call**:

```python
def smart_generate_image(self, prompt: str, __id__: str = "") -> str:
    ...
```

Task list:
- [x] Add `_load_workflow(tool_id, filename)` helper
- [x] Apply to `smart_generate_image.py`
- [x] Apply to `enhance_image.py`
- [x] Apply to `generate_video.py`

### ✅ Milestone 2 — Remove embedded workflow (completed)

All embedded JSON constants and their `json.loads()` calls have been removed
from the three scripts. In their place:

- `_load_workflow(tool_id, filename)` — loads from cache, raises if missing
- `_resolve_node(workflow, title)` — finds nodes by `_meta.title`
- `_inject_placeholders()` removed — all values are injected post-parse

**Lines saved per script:**

| Script | Lines removed |
|---|---|
| `enhance_image.py` | ~121 |
| `generate_video.py` | ~454 |
| `smart_generate_image.py` | ~267 |

**Additional improvements made during this milestone:**
- Workflow JSONs are now clean (no `{{PROMPT}}` / `{{SEED}}` placeholders)
- Node resolution by `_meta.title` instead of fragile numeric IDs

Task list:
- [x] Update `_load_workflow()` to raise a clear error if file not found
- [x] Remove `_ENHANCE_WORKFLOW_JSON_RAW` from `enhance_image.py`
- [x] Remove `_VIDEO_WORKFLOW_JSON_RAW` from `generate_video.py`
- [x] Remove `_ZIT_WORKFLOW_JSON_RAW` from `smart_generate_image.py`
- [x] Remove Node ID constants sections
- [x] Remove `{{PROMPT}}` and `{{SEED}}` from all workflow JSONs
- [x] Add `_resolve_node()` for title-based node lookup

### ❌ Milestone 3 — Keep `workflows/` as source of truth (cancelled)

`workflows/` already serves as the canonical source. No Makefile or sync
script is needed — admins copy JSONs manually on first deploy (documented
in README).

- [x] `workflows/` files already renamed and in sync
- [ ] ~~Optionally add a Makefile or script target~~ → Cancelled

### ✅ Milestone 4 — Update README (completed)

- [x] Add a section explaining the workflow loading strategy
- [x] Document how to update a workflow in production (copy JSON → cache dir)
- [x] Update the Workflows table to reflect the new purpose of `workflows/`
- [x] Add FAQ entries for missing workflow file and workflow update

### ✅ Milestone 5 — Cleanup and verification (completed)

- [x] Verify that `workflows/` JSON files are valid (no placeholders, parseable)
- [x] Verify that changing `workflows/enhance_image.json` takes effect after
      copying to the cache dir (tested in production)
- [x] Verify the same for `generate_video` and `smart_generate_image`
- [x] All three tools confirmed working in production

---

## Implementation notes

### `CACHE_DIR` path resolution

`CACHE_DIR` is defined in `open_webui/config.py` (line 176):

```python
CACHE_DIR = DATA_DIR / 'cache'
```

Tools can import it directly — Open WebUI's `replace_imports()`
automatically translates `from config import ...` to
`from open_webui.config import ...`:

```python
from open_webui.config import CACHE_DIR
from pathlib import Path

workflow_path = CACHE_DIR / 'tools' / 'smart_generate_image' / 'workflow.json'
```

In a Docker deployment `DATA_DIR` defaults to `/app/backend/data/`, so the
full path resolves to:

```
/app/backend/data/cache/tools/<tool_id>/workflow.json
```

### Alternative: HTTP access via `GET /cache/{path}`

The same files are also served over HTTP with `get_verified_user` auth:

```
GET /cache/tools/<tool_id>/workflow.json
```

This is useful if the tool needs to expose a downloadable URL. The endpoint
is defined in `open_webui/main.py`.

### Persistence

- The `cache/tools/<id>/` directory survives container restarts because it
  lives under `DATA_DIR` (typically a Docker volume mounted at
  `/app/backend/data/`).
- When a tool is updated via `POST /api/v1/tools/id/<id>/update`, Open WebUI
  does **not** recreate the cache directory — it already exists.
- Bootstrap on first run ensures zero-config deployment: paste the tool,
  use it, and the workflow JSON appears in the cache automatically.

---

## User valve dropdown cleanup

User dropdowns should not include a "System default" / "Model default" option
because `default=""` (or `default="0"`) in the Field already handles the
"use admin value" case. Having it in the dropdown is redundant and confusing.

### smart_generate_image.py
- `UserValves.model_family`: remove `""` / `"System default"` from `_MODEL_FAMILY_OPTIONS`
- `UserValves.steps`: remove `"0"` / `"Model default"` from `_STEPS_OPTIONS`

### edit_image.py
- `UserValves.steps`: remove `"0"` / `"0 (System default)"` from `_STEPS_OPTIONS`

### Already fixed
- `generate_caption.py` (branch `feat/generate-caption`)
- `generate_video.py` (branch `feat/generate-caption` — `UserValves.length`)
