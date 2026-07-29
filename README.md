# Open WebUI Comfy Tools

A collection of Python tools that integrate ComfyUI workflows into Open WebUI chat sessions. Each tool loads its corresponding workflow JSON from a cache directory and communicates with a ComfyUI server via its REST API. Tools support configurable valves at both admin and user level.

## Tools

### Smart Generate Image

Generates images from a text prompt. Supports three model families: Z-Image Turbo, Krea 2, and FLUX.2 Klein. Each model family defines its own defaults for model file, VAE, scheduler, sampler, and CFG scale.

**Admin valves.** Model family, specific model file override, default aspect ratio, megapixel target, max steps, seed, and LoRA configuration.

**User valves.** Model family, model name, inference steps, seed, and LoRAs.

### Edit Image

Edits a previously generated image using Flux 2 inpainting. Accepts either a tool-generated filename or an external URL.

**Admin valves.** Default steps and LoRA configuration.

**User valves.** Steps and LoRAs.

### Generate Caption

Generates a caption for an image using Florence-2 via ComfyUI. The LLM calls this tool automatically when it needs to interpret image content before editing or answering questions.

**Admin valves.** Model, task type (detailed, caption, OCR, etc.), max new tokens, num beams, do_sample, and seed.

**User valves.** Model, task, max new tokens, num beams, do_sample, and seed.

The caption is always returned in English for accuracy. The LLM translates it when replying in the user's language.

### Enhance Image

Upscales or enhances an image using SeedVR2. Loads images via URL using the ComfyUI-LoadImageURL custom node.

**Admin valves.** ComfyUI base URL override.

### Generate Video

Generates videos from text or images using Wan 2.1 (single-path) or Wan 2.2 (dual-path high/low resolution). Frames follow a 4n+1 constraint imposed by the WAN temporal VAE stride.

**Admin valves.** Model version, diffusion model config, LoRAs, frame count, negative prompt, steps, seed.

**User valves.** Model version, frame count, steps, negative prompt, seed.

The result renders as an HTML video element in the chat with autoplay, muted, and loop attributes.

## Configuration

Each tool exposes valves in two tiers:

- **Admin valves** - set in Workspace > Tools. Define defaults and limits for all users.
- **User valves** - set per chat session. Override admin defaults without affecting other users.

### Smart Generate Image

| Valve | Level | Description |
|---|---|---|
| Model family | Admin, User | zit, krea2, or flux.2 |
| Specific model | Admin, User | Override the .safetensors file within the family |
| Steps | Admin, User | Inference steps. 0 = use model family default |
| Seed | Admin, User | -1 = random, >=0 = fixed |
| Aspect ratio | Admin | Width:height ratio (e.g., 2:3, 16:9) |
| Megapixel | Admin | Resolution target (default 1.0) |
| Max steps | Admin | Cap on user steps. -1 = force model default |
| LoRAs | Admin, User | JSON array of LoRA configs |

### Edit Image

| Valve | Level | Description |
|---|---|---|
| Steps | Admin, User | 0 = use workflow default (6) |
| LoRAs | Admin, User | JSON array, applied in order to lora_1..lora_N |

### Generate Caption

| Valve | Level | Description |
|---|---|---|
| Model | Admin, User | Florence-2 variant |
| Task | Admin, User | caption, detailed_caption, ocr, etc. |
| Max new tokens | Admin, User | Maximum caption length |
| Num beams | Admin, User | Beam search width |
| do_sample | Admin, User | Sampling vs. greedy decoding |
| Seed | Admin, User | -1 = random, >=0 = fixed |

### Enhance Image

| Valve | Level | Description |
|---|---|---|
| Base URL | Admin | Override the ComfyUI server URL |

### Generate Video

| Valve | Level | Description |
|---|---|---|
| Model version | Admin, User | wan21 or wan22 |
| Diffusion model | Admin | JSON specifying concrete model files |
| LoRAs | Admin | Applied per path (high/low for Wan 2.2) |
| Frames | Admin, User | Video length. Must be 4n+1 (81-161) |
| Negative prompt | Admin, User | Content to exclude |
| Steps | Admin, User | 4-10 |
| Seed | Admin, User | -1 = random, >=0 = fixed |

## Installation

### 1. Add the tools in Open WebUI

Navigate to Workspace > Tools, click "+", paste the script content, and save.

| Script | Suggested name |
|---|---|
| smart_generate_image/tool.py | Smart Generate Image |
| enhance_image/tool.py | Enhance Image |
| edit_image/tool.py | Edit Image |
| generate_caption/tool.py | Generate Caption |
| generate_video/tool.py | Generate Video |

### 2. Deploy the workflow JSONs

Each tool requires its corresponding workflow JSON file. Copy it from the tool's directory to the tool's cache directory inside the Open WebUI container:

```
cp smart_generate_image/smart_generate_image.json /app/backend/data/cache/tools/smart_generate_image/smart_generate_image.json
cp edit_image/edit_image.json                     /app/backend/data/cache/tools/edit_image/edit_image.json
cp enhance_image/enhance_image.json               /app/backend/data/cache/tools/enhance_image/enhance_image.json
cp generate_caption/generate_caption.json         /app/backend/data/cache/tools/generate_caption/generate_caption.json
cp generate_video/generate_video.json             /app/backend/data/cache/tools/generate_video/generate_video.json
cp generate_video/generate_video_wan22.json       /app/backend/data/cache/tools/generate_video/generate_video_wan22.json
```

The cache/tools/<name>/ directory is created automatically when you save the tool script.

### 3. Enable the tools

In any chat, open the tool selector and enable the ones you want to use.

### Requirements

- Open WebUI (recent version with native Tools support)
- A running ComfyUI server
- Native Tool Calling enabled
- Image Generation Engine set to ComfyUI (Admin Panel > Settings > Images)
- ComfyUI-LoadImageURL custom node (required by Enhance Image)

## FAQ

**The image does not appear in the chat.** Verify that the ComfyUI Base URL is reachable from your browser. Images are served directly from ComfyUI.

**Seed appears to have no effect.** Configure a "seed" node in Admin Panel > Settings > Images > ComfyUI Workflow Nodes.

**What is the difference between model_family and model_name?** model_family selects a full configuration (model, VAE, scheduler, sampler, CFG). model_name overrides only the .safetensors file within that family.

**Steps are ignored.** The admin may have max_steps set to -1 (force model default). They should set it to 0 or a specific value.

**Enhance Image fails with an image loading error.** Ensure ComfyUI-LoadImageURL is installed in ComfyUI's custom_nodes/ directory.

**The tool fails with "Workflow file not found".** The JSON file must be copied to the tool's cache directory. See the installation section above.

**How do I update a workflow without editing the tool?** Replace the JSON file in cache/tools/<id>/ and the tool loads the new version on the next run.

**Generate Caption always returns English text.** The caption is generated in English for technical accuracy. The LLM translates it when replying to you.

## Project structure

```
smart_generate_image/
  tool.py                    -- Image generation
  smart_generate_image.json  -- ComfyUI workflow
enhance_image/
  tool.py                    -- Image upscale/enhance
  enhance_image.json         -- ComfyUI workflow
edit_image/
  tool.py                    -- Image editing (Flux 2)
  edit_image.json            -- ComfyUI workflow
generate_caption/
  tool.py                    -- Image captioning (Florence-2)
  generate_caption.json      -- ComfyUI workflow
generate_video/
  tool.py                    -- Video generation (Wan 2.1 / 2.2)
  generate_video.json        -- ComfyUI workflow (Wan 2.1)
  generate_video_wan22.json  -- ComfyUI workflow (Wan 2.2)
README.md
```
