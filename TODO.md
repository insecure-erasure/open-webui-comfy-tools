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
- **Workflows directory:** `workflows/` (already renamed to match script names)
- **Embedded workflows are currently in sync** with `workflows/*.json` —
  verified by the diff analysis done in this session.
- The embedded JSONs use `{{PROMPT}}` and `{{SEED}}` as placeholders that
  get replaced at runtime via `.replace()`.
- LoRAs in the embedded workflows are currently empty/disabled (`on: false`,
  `lora: ""`, `strength: 0`), except for `generate_video.py` where LoRA was
  recently disabled by commit `08ded78`.

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

#### Key: how to get the tool's ID at runtime

Open WebUI injects `__id__` as a hidden parameter **into each tool function
call**.  It is NOT available at class level or in `__init__` — only inside
the function bodies that the LLM invokes.  The path must be resolved inside
the function that generates the image/video.

```python
def generate(self, prompt: str, __id__: str = "") -> str:
    cache_dir = CACHE_DIR / 'tools' / __id__
    workflow_path = cache_dir / 'workflow.json'
```

#### File naming convention

| Location | Filename |
|---|---|
| `workflows/` in Git | `enhance_image.json`, `generate_video.json`, `smart_generate_image.json` |
| `cache/tools/<id>/` at runtime | `workflow.json` (always, regardless of tool) |

- [ ] Add `_load_workflow(__id__: str)` helper that:
      1. Resolves `CACHE_DIR / 'tools' / __id__ / 'workflow.json'`
      2. If the file exists on disk → read and return it
      3. If missing → write the embedded JSON there (bootstrap), then read it
- [ ] Apply to `smart_generate_image.py` — replace `json.loads(_ZIT_WORKFLOW_JSON_RAW)`
- [ ] Apply to `enhance_image.py` — replace `json.loads(_ENHANCE_WORKFLOW_JSON_RAW)`
- [ ] Apply to `generate_video.py` — replace `json.loads(_VIDEO_WORKFLOW_JSON_RAW)`

### Milestone 2 — Remove embedded workflow (optional per tool)

Once bootstrapping works reliably, the embedded JSON can be dropped,
leaving only a minimal fallback or an error if the file is missing.

**Current embedded variable names (to be removed):**

| Script | Variable to remove | Lines saved |
|---|---|---|
| `enhance_image.py` | `_ENHANCE_WORKFLOW_JSON_RAW` | ~115 |
| `generate_video.py` | `_VIDEO_WORKFLOW_JSON_RAW` | ~410 |
| `smart_generate_image.py` | `_ZIT_WORKFLOW_JSON_RAW` | ~213 |

- [ ] Update `_load_workflow()` to raise / log a clear error if the file
      is not found and there is no embedded fallback
- [ ] Remove the `_*_WORKFLOW_JSON_RAW` constant and its `json.loads()` line
      from each script
- [ ] Remove the `# --- Node ID constants ---` section if workflow-specific
      (only the embedded JSON references them)

### Milestone 3 — Keep `workflows/` as the source of truth for Git

The `workflows/` directory stays in the repo.  It holds the canonical JSON
files that should be synced to the container's `cache/tools/<id>/`.

**Current state:** `workflows/` contains:
- `enhance_image.json`
- `generate_video.json`
- `smart_generate_image.json`

(Already renamed and in sync with the embedded code.)

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
- [ ] Run the existing comparison script to confirm `workflows/` JSONs
      match the embedded fallbacks:
      ```bash
      cd /srv/pi/smart_generate_image && python3 << 'PYEOF'
      import re, json
      def extract_raw(filepath, varname):
          content = open(filepath).read()
          pattern = rf'{varname}\s*=\s*r"""(.*?)"""\s*\n'
          m = re.search(pattern, content, re.DOTALL)
          return json.loads(m.group(1)) if m else None
      def normalize(s):
          return json.dumps(json.loads(s), indent=2, sort_keys=True)
      for script, var, wf_file in [
          ('enhance_image.py', '_ENHANCE_WORKFLOW_JSON_RAW', 'workflows/enhance_image.json'),
          ('generate_video.py', '_VIDEO_WORKFLOW_JSON_RAW', 'workflows/generate_video.json'),
          ('smart_generate_image.py', '_ZIT_WORKFLOW_JSON_RAW', 'workflows/smart_generate_image.json'),
      ]:
          emb = normalize(json.dumps(extract_raw(script, var)))
          fle = normalize(open(wf_file).read())
          ok = "✅" if emb == fle else "❌"
          print(f'{ok} {script} ↔ {wf_file}')
      PYEOF
      ```

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
