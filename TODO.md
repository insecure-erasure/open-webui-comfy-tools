# TODO: Smart Generate Image v3.0 — Refactor

Remove monkey patches and Admin UI dependencies from Open WebUI.
Inline workflow, inject values directly into the JSON dict.

## Milestones

### Milestone 1 — Inline workflow + node IDs ✅
- [x] Embed `zit.json` as a raw string at the top of the script
- [x] Define node ID constants (KSampler, CLIPTextEncode, etc.)
- [x] Remove monkey patch assignment lines (1, 2 and 3)
- [x] Remove unused import (`comfyui_module`)

### Milestone 2 — Update Admin Valves ✅
- [x] Add `max_steps` (dropdown 0-15, default "0")
- [x] Add `default_size` (string, default "1024x1024")
- [x] Update descriptions to remove Admin UI references

### Milestone 3 — Update User Valves ✅
- [x] Add `lora_name` (string, default "")
- [x] Add `lora_strength` (float, default 0.0)
- [x] Update model_name description (remove Admin UI reference)

### Milestone 4 — Rewrite generation logic ✅
- [x] Rewrite the function that builds the workflow with injected values
- [x] Inject prompt, model, steps, seed, size and LoRA directly into the dict
- [x] Use direct ComfyUI API (queue_prompt + wait_for_output) — no Open WebUI dependency
- [x] Keep GCD reduction for aspect ratio
- [x] Add `_inject_placeholders()`, `_comfyui_queue_prompt()`, `_comfyui_wait_for_output()`, `_comfyui_interrupt()`, `_extract_image_filename()` helper functions
- [x] Remove all monkey-patch code (PatchedCreateImageForm, patched_apply_workflow_nodes, patched_image_generations)
- [x] Clean unused imports (`urlparse`, `parse_qs`, duplicate `uuid`)

### Milestone 5 — Final cleanup
- [ ] Verify no references to `COMFYUI_WORKFLOW` or `COMFYUI_WORKFLOW_NODES` remain
- [ ] Verify no references to `IMAGE_SIZE` or `IMAGE_STEPS` remain
- [ ] Verify `COMFYUI_BASE_URL` and `COMFYUI_API_KEY` are still read
- [ ] Test steps logic with `max_steps=0` (forces workflow default)
- [ ] Test steps logic with `max_steps>0` (normal clamping)
- [ ] Test LoRA with empty values and with real values
- [ ] Test cancellation (interrupt)
