# Smart Generate Image

Open WebUI tool for AI image generation through ComfyUI -- with control over seed, model, size, and steps, directly from your chat.

> **Compatible with:** Open WebUI + ComfyUI

---

## Overview

When you ask your AI assistant to generate an image, **Smart Generate Image** takes over and sends your request straight to ComfyUI. The image appears right in the chat conversation -- no extra clicks, no separate tools.

### Key features

- **Seed control** -- Same prompt + same seed = same image every time. Useful for iterating on a design without losing the look you liked.
- **Smart dimensions** -- You can ask for `2000x3000` and the tool automatically converts it to the right aspect ratio for your ComfyUI workflow (no more "invalid size" errors).
- **AI-enhanced prompts** -- The AI enriches your description with helpful visual details, and translates it to English if needed.
- **No local storage** -- Images are served directly from ComfyUI. No unnecessary downloads or re-uploads, keeping your server disk clean.
- **Works with your existing setup** -- All settings (model, image size, steps) come from your Open WebUI Admin panel. No extra configuration needed.

---

## Relationship with native image generation tool

Open WebUI has two image generation tools available to the AI:

| Feature | Native `generate_image` | Smart Generate Image |
|---------|------------------------|---------------------|
| **Trigger** | Click the image chip in the chat input | The AI calls it automatically when you ask for an image |
| **Seed control** | Not available | Yes -- reproducible images |
| **Model override** | Admin default only | Ask for a specific model |
| **Aspect ratio** | Fixed or admin-set | Smart GCD conversion |
| **Steps override** | Admin default only | Override per request |

When **Native Tool Calling** is enabled in Open WebUI, the built-in `generate_image` function is available to the AI alongside Smart Generate Image. The AI will decide which one to use based on your request -- if you need seed, model, or steps control, it will use Smart Generate Image. For simple requests without those parameters, it may use the native `generate_image`.

---

## Installation

1. In Open WebUI, go to **Workspace -> Tools**.
2. Click **"+"** to create a new tool.
3. Paste the contents of `smart_generate_image.py`.
4. Save it with the title **"Smart Generate Image"**.
5. Go to any chat, open the tool selector, and enable **Smart Generate Image**.

---

## Configuration

Everything is configured through the **Open WebUI Admin panel -> Settings -> Images**:

- **Image Generation Engine** -- Set to **ComfyUI**.
- **ComfyUI Base URL** -- The address of your ComfyUI server (must be reachable from your browser).
- **ComfyUI Workflow** -- Your exported workflow JSON.
- **ComfyUI Workflow Nodes** -- Define which nodes receive prompt, seed, model, steps, etc.
- **Image Size** -- Default dimensions (user can override).
- **Image Steps** -- Default inference steps (user can override).

> If you want seed support, make sure your workflow has a **"seed"** node configured in the Workflow Nodes section.

---

## Usage

Just ask the AI to create an image naturally:

> *"Generate an image of a cat wearing a spacesuit on Mars"*
>
> *"Create a 1920x1080 landscape of a cyberpunk city at night"*
>
> *"Make another one like the previous image but with a different seed"*

The AI will:

1. Translate your request to English if needed.
2. Enrich the prompt with visual details (without changing your subject).
3. Send it to ComfyUI with the appropriate settings.
4. Display the image directly in the chat.

### Optional parameters

You can also be more specific:

| You say | What happens |
|---------|-------------|
| *"... use model sdxl_ turbo"* | The tool switches to that model |
| *"... at 2000x3000"* | Dimensions are converted to your workflow's aspect ratio |
| *"... with 30 steps"* | Overrides the default step count |
| *"... with seed 42"* | Locks the random seed for reproducibility |

---

## Requirements

- Open WebUI (any recent version with Tools support)
- ComfyUI server running and accessible
- Native Tool Calling enabled in Open WebUI settings
- A ComfyUI workflow exported and configured in Admin -> Images

---

## FAQ

**Q: The image doesn't show up in the chat.**  
A: Make sure your `ComfyUI Base URL` is accessible from your browser. The image is served directly from ComfyUI, not stored locally.

**Q: Seed doesn't seem to do anything.**  
A: Your workflow needs a "seed" node configured in **Admin -> Images -> ComfyUI Workflow Nodes**. Without it, the seed value is ignored and ComfyUI uses its own random seed.


