# TODO

## Video Generation (WAN2.1) - Implemented

`generate_video.py` is complete:

- Embedded real WAN2.1 I2V workflow (`wan21_i2v.json` as reference)
- Direct ComfyUI communication via HTTP (`POST /prompt` + `GET /history/{id}`)
- Placeholders `{{PROMPT}}`, `{{SEED}}`, `{{IMAGE}}`
- Valves: model, lora, length, negative_prompt, seed, comfyui_image_base_url
- Resolution: UserValves > AdminValves > workflow default
- Interrupt on cancellation
- ~10 min timeout with polling
- HTML block for video playback

Pending:
- [ ] End-to-end test with real ComfyUI

## Refactor pending (smart_generate_image.py)

Remove the 3 monkey patches and replace with direct HTTP communication to ComfyUI:

- Read workflow from `get_image_config()` (public API)
- Implement own `POST /prompt` + `GET /history/{id}`
- Keep valves for model, steps, seed and base URL
- Keep ephemeral behavior (return text, do not use event emitter)
