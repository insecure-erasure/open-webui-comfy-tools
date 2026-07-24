# TODO: Smart Generate Image v3.0 — Refactor

Remove monkey patches and Admin UI dependencies from Open WebUI.
Inline workflow, inject values directly into the JSON dict.

## Milestones

### Milestone 1 — Inline workflow + node IDs ✅
- [x] Embed `zit.json` as a raw string at the top of the script
- [x] Define node ID constants (KSampler, CLIPTextEncode, etc.)
- [x] Remove monkey patch assignment lines (1, 2 and 3)
- [x] Remove unused import (`comfyui_module`)

### Milestone 2 — Update Admin Valves
- [ ] Add `max_steps` (dropdown 0-15, default "0")
- [ ] Add `default_size` (string, default "1024x1024")

### Milestone 3 — Update User Valves
- [ ] Add `lora_name` (string, default "")
- [ ] Add `lora_strength` (float, default 0.0)

### Milestone 4 — Rewrite generation logic
- [ ] Rewrite the function that builds the workflow with injected values
- [ ] Inject prompt, model, steps, seed, size and LoRA directly into the dict
- [ ] Call ComfyUI with `nodes: []` (like enhance_image.py)
- [ ] Keep GCD reduction for aspect ratio

### Milestone 5 — Final cleanup
- [ ] Verify no references to `COMFYUI_WORKFLOW` or `COMFYUI_WORKFLOW_NODES` remain
- [ ] Verify no references to `IMAGE_SIZE` or `IMAGE_STEPS` remain
- [ ] Verify `COMFYUI_BASE_URL` and `COMFYUI_API_KEY` are still read
- [ ] Test steps logic with `max_steps=0` (forces workflow default)
- [ ] Test steps logic with `max_steps>0` (normal clamping)
- [ ] Test LoRA with empty values and with real values
- [ ] Test cancellation (interrupt)
