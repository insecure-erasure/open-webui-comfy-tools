# Smart Generate Image

Open WebUI tool for AI image generation through ComfyUI -- with size control, configurable model, steps and seed via Valves.

> **Compatible with:** Open WebUI + ComfyUI

---

## Overview

Smart Generate Image is a tool that sends image generation requests to ComfyUI. The image appears in the chat rendered as markdown by the LLM. No local storage, no event emitter for files, no database persistence.

It is installed as a regular user tool in **Workspace -> Tools**. It does not require Image Generation to be enabled as a model capability or builtin tool. It uses the image generation settings from **Admin Panel -> Settings -> Images** when the engine is set to ComfyUI.

---

## Tool parameters

The tool exposes two parameters to the LLM:

| Parameter | Type | Description |
|-----------|------|-------------|
| prompt | string | Image description. The LLM translates to English and enriches with visual details. |
| size | string (optional) | Dimensions as WxH (e.g. 2000x3000). Falls back to Admin UI config if omitted. |

Model, steps, and seed are configured via Valves.

---

## Valves

### Admin Valves

Configurable by admins in **Workspace -> Tools -> Smart Generate Image -> Valves**.

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| model_name | string | "" | Model/checkpoint name. Overrides the Admin UI default. Leave empty to use the Admin UI setting. |
| steps | dropdown | 0 (Inherit) | Inference steps (1-15, descending). 0 = inherit from Admin UI or use workflow default. |

### User Valves

Configurable by end users from the chat interface.

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| model_name | string | "" | Your preferred model/checkpoint. Overrides the admin valve and the Admin UI setting. |
| steps | dropdown | 0 (Inherit) | Inference steps (1-15, descending). 0 = inherit from admin valve or Admin UI setting. |
| seed | int | -1 | Seed. -1 = random, >=0 = fixed seed for reproducibility. |

### Precedence

**Model resolution:** UserValves > AdminValves > Admin UI (get_image_model) > None

**Steps resolution:** UserValves > AdminValves > Admin UI (IMAGE_STEPS) > workflow default

**Seed resolution:** UserValve. -1 = random (auto-generated), >=0 = fixed.

Both UserValves and AdminValves are clamped against the Admin UI IMAGE_STEPS ceiling (or 15 as safety fallback). When clamping occurs, a warning toast is shown to the user.

---

## Configuration

Configure these in **Admin Panel -> Settings -> Images**:

- Image Generation Engine: ComfyUI
- ComfyUI Base URL: your ComfyUI server address (must be browser-accessible)
- ComfyUI Workflow: exported workflow JSON
- ComfyUI Workflow Nodes: define which nodes receive each parameter
- Image Size: default dimensions
- Image Steps: default inference steps (acts as ceiling for Valves)

For seed support, configure a "seed" node in Workflow Nodes.

---

## Installation

1. Go to **Workspace -> Tools** in Open WebUI.
2. Click **"+"** and paste the contents of `smart_generate_image.py`.
3. Save as **"Smart Generate Image"**.
4. Enable the tool in the chat tool selector.

---

## Requirements

- Open WebUI (any recent version with Tools support)
- ComfyUI server running and accessible
- Native Tool Calling enabled
- ComfyUI workflow configured in Admin Panel -> Settings -> Images
- Image Generation Engine set to ComfyUI
- Image Generation does NOT need to be enabled as a model capability or builtin tool

---

## Usage

Ask the AI to create an image naturally:

> *"Generate an image of a cat wearing a spacesuit on Mars"*
>
> *"Create a 1920x1080 landscape of a cyberpunk city at night"*

Optional details the AI can handle:

- **Size**: "... at 2000x3000"

Model, steps, and seed are controlled via Valves and Admin UI settings, not from the chat prompt.

---

## FAQ

**Q: The image doesn't show up in the chat.**  
A: Check that the ComfyUI Base URL is accessible from your browser. Images are served directly from ComfyUI.

**Q: Seed doesn't seem to do anything.**  
A: Configure a "seed" node in Admin Panel -> Settings -> Images -> ComfyUI Workflow Nodes.

**Q: How do I change the model or steps?**  
A: Admins configure them in Workspace -> Tools -> Smart Generate Image -> Valves. Users can override from the chat interface. Alternatively, set defaults in Admin Panel -> Settings -> Images.
