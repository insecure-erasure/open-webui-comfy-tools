# Open WebUI Comfy Tools

A set of AI tools to generate, edit, enhance and caption images — and generate videos — directly in your Open WebUI chats, powered by ComfyUI.

---

## 🧰 Tools

### 🎨 Smart Generate Image
Generates images from text with size control, model selection, and seed support.

**Example prompts:**
> *"Generate an image of a cat wearing a spacesuit on Mars"*
> *"Create a 1920x1080 landscape of a cyberpunk city at night"*

**Output:** the image renders inline in the chat.

**Available models:** Z-Image Turbo, FLUX.2 Klein, and Krea 2 Turbo.

---

### ✏️ Edit Image
Edits a previously generated image using Flux 2. Make targeted changes without regenerating from scratch.

**Example prompts:**
> *"Edit that image — change the background to a beach at sunset"*
> *"Add a dragon flying over the mountain in the last image"*

Accepts both tool-generated filenames and external URLs.

---

### 🔍 Generate Caption
Generates a detailed caption of an image using Florence-2. The LLM uses this automatically to "see" image content before editing it or answering questions about it.

**Example prompts (indirect):**
> *"What's in this image?"* → the LLM calls the tool and replies in your language
> *"Describe the last image in detail"*

The caption is always generated in English for accuracy; the LLM translates it when replying.

---

### 🔬 Enhance Image
Upscales or enhances an image using SeedVR2.

**Example prompts:**
> *"Enhance that image"*
> *"Upscale the last image to higher resolution"*

---

### 🎬 Generate Video
Generates videos from text or images using Wan 2.1 / 2.2.

**Example prompts:**
> *"Generate a video of a cat walking on the moon"*
> *"Animate the last image — make the waves move"*

The result appears as an HTML video player in the chat (autoplay, muted, loop).

---

## ⚙️ Configuration

Each tool has configurable options accessible through the **Valves** panel in Open WebUI. There are two levels:

- **Admin Valves** — set by the admin in Workspace → Tools (defaults and limits)
- **User Valves** — set by each user from the chat (personal preferences)

### Smart Generate Image

| What you can tweak | How it works |
|---|---|
| **Model family** | Pick Z-Image Turbo, FLUX.2 Klein, or Krea 2 Turbo |
| **Specific model** | A custom .safetensors filename within the family |
| **Steps** | Inference steps. 0 = use model family default |
| **Seed** | -1 = random, ≥0 = fixed for reproducibility |
| **LoRAs** | JSON array of LoRAs for style tuning |

### Edit Image

| What you can tweak | How it works |
|---|---|
| **Steps** | 0 = use workflow default (6) |
| **LoRAs** | JSON array of LoRAs, applied positionally to lora_1..lora_N |
| **Base URL** | Override the image link base URL if needed |

### Generate Caption

| What you can tweak | How it works |
|---|---|
| **Model** | Florence-2 variant (base, large, nsfw, etc.) |
| **Task** | Caption type: detailed, OCR, caption, etc. |
| **Max new tokens** | Max caption length |
| **Num beams** | Beam search width — more beams = better quality, slower |
| **do_sample** | On = more creative; off = more deterministic (greedy) |
| **Seed** | -1 = random, ≥0 = fixed for reproducibility |

### Enhance Image

| What you can tweak | How it works |
|---|---|
| **Base URL** | Only if you need to override the ComfyUI server URL |

### Generate Video

| What you can tweak | How it works |
|---|---|
| **Model version** | Wan 2.1 (single-path) or Wan 2.2 (dual-path high/low) |
| **Diffusion model** | JSON specifying concrete model(s) |
| **LoRAs** | LoRAs applied per path (high/low) |
| **Frames (length)** | Video duration in frames. Must be 4n+1 |
| **Negative prompt** | What you don't want to see in the video |
| **Steps** | 4-10 inference steps |
| **Seed** | -1 = random, ≥0 = fixed |

---

## 🚀 Installation

### 1. Add the tools in Open WebUI

Go to **Workspace → Tools**, click **"+"**, paste the script content, and save with the suggested name:

| Script | Suggested name |
|--------|----------------|
| `smart_generate_image.py` | Smart Generate Image |
| `enhance_image.py` | Enhance Image |
| `edit_image.py` | Edit Image |
| `generate_caption.py` | Generate Caption |
| `generate_video.py` | Generate Video |

### 2. Deploy the workflow JSONs

Each tool needs its workflow JSON file. You'll find them in the `workflows/` directory. Copy them to the tool's cache directory inside the Open WebUI container:

```bash
# Example for Smart Generate Image
cp workflows/smart_generate_image.json /app/backend/data/cache/tools/smart_generate_image/smart_generate_image.json

# Repeat for each tool:
cp workflows/edit_image.json       /app/backend/data/cache/tools/edit_image/edit_image.json
cp workflows/enhance_image.json    /app/backend/data/cache/tools/enhance_image/enhance_image.json
cp workflows/generate_caption.json /app/backend/data/cache/tools/generate_caption/generate_caption.json
cp workflows/generate_video.json   /app/backend/data/cache/tools/generate_video/generate_video.json
cp workflows/generate_video_wan22.json /app/backend/data/cache/tools/generate_video/generate_video_wan22.json
```

> The `cache/tools/<name>/` directory is created automatically when you save the tool.

### 3. Enable the tools

In any chat, open the tool selector and enable the ones you want to use.

### Requirements

- Open WebUI (recent version with Tools support)
- A running ComfyUI server
- Native Tool Calling enabled
- Image Generation Engine set to ComfyUI (Admin Panel → Settings → Images)
- [ComfyUI-LoadImageURL](https://github.com/insecure-erasure/ComfyUI-LoadImageURL) custom node (required by Enhance Image)

---

## ❓ FAQ

**The image doesn't show up in the chat.**
Check that the ComfyUI Base URL is accessible from your browser. Images are served directly from ComfyUI.

**Seed doesn't seem to do anything.**
Configure a "seed" node in Admin Panel → Settings → Images → ComfyUI Workflow Nodes.

**How do I change the model or steps?**
Use the User Valves from the tool's configuration panel in the chat.

**What's the difference between model_family and model_name?**
`model_family` selects a full configuration (model, VAE, scheduler, sampler, CFG). `model_name` only overrides the .safetensors file within that family. Usually you just need to change the family.

**I changed steps but nothing happened.**
The admin may have `max_steps` set to `-1` (force model default). Ask them to set it to `0` or a specific value.

**Enhance Image fails with an image loading error.**
Make sure [ComfyUI-LoadImageURL](https://github.com/insecure-erasure/ComfyUI-LoadImageURL) is installed in ComfyUI's `custom_nodes/` directory.

**The tool fails with "Workflow file not found".**
The JSON file needs to be copied to the tool's cache directory. See the [installation section](#2-deploy-the-workflow-jsons).

**How do I update a workflow without editing the tool?**
Just replace the JSON file in `cache/tools/<id>/` and the tool will pick it up on the next run. No need to edit the script.

**Generate Caption always returns English text — why?**
For technical accuracy. The LLM receives the caption in English and translates it when replying to you.

---

## 📂 Project structure

```
smart_generate_image/
├── smart_generate_image.py   → Image generation
├── enhance_image.py          → Image upscale/enhance
├── edit_image.py             → Image editing (Flux 2)
├── generate_caption.py       → Image captioning (Florence-2)
├── generate_video.py         → Video generation (Wan)
├── workflows/                → Workflow JSON files (copy to server)
│   ├── smart_generate_image.json
│   ├── enhance_image.json
│   ├── edit_image.json
│   ├── generate_caption.json
│   ├── generate_video.json
│   └── generate_video_wan22.json
└── README.md
```
