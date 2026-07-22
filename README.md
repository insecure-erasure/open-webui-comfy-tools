# Image Generator Pro

A smart Open WebUI tool for AI image generation through ComfyUI — with full control over seed, model, size, and steps, directly from your chat.

> **Compatible with:** Open WebUI + ComfyUI  
> **Also works with:** OpenAI DALL-E, Google Gemini, AUTOMATIC1111 (seed is ignored on those engines)

---

## ✨ What does it do?

When you ask your AI assistant to generate an image, **Image Generator Pro** takes over and sends your request straight to ComfyUI. The image appears right in the chat conversation — no extra clicks, no separate tools.

### What makes it special?

- **🎯 Seed control** — Same prompt + same seed = same image every time. Perfect for iterating on a design without losing the look you liked.
- **📐 Smart dimensions** — You can ask for `2000x3000` and the tool automatically converts it to the right aspect ratio for your ComfyUI workflow (no more "invalid size" errors).
- **🤖 AI-enhanced prompts** — The AI enriches your description with helpful visual details, and translates it to English if needed.
- **⚡ No local storage** — Images are served directly from ComfyUI. No unnecessary downloads or re-uploads, keeping your server disk clean.
- **🔧 Works with your existing setup** — All settings (model, image size, steps) come from your Open WebUI Admin panel. No extra configuration needed.

---

## 🧩 How it replaces the native image tool

Open WebUI has two ways of generating images:

| Feature | Native 📷 (chip icon) | Image Generator Pro ⚙️ (tool selector) |
|---------|----------------------|----------------------------------------|
| **Trigger** | Click the image chip in the chat input | The AI calls it automatically when you ask for an image |
| **Seed control** | ❌ Not available | ✅ Yes — reproducible images |
| **Model override** | ❌ Admin default only | ✅ Ask for a specific model |
| **Aspect ratio** | Fixed or admin-set | ✅ Smart GCD conversion |
| **Steps override** | ❌ Admin default only | ✅ Override per request |

When **Native Tool Calling** is enabled in Open WebUI, the AI will automatically use Image Generator Pro whenever you ask for an image — just describe what you want and the AI handles the rest.

> 💡 **Tip:** The native 📷 chip still works independently. You can keep both enabled — the AI will choose the right one.

---

## 🚀 Installation

1. In Open WebUI, go to **Workspace → Tools**.
2. Click **"+"** to create a new tool.
3. Paste the contents of `smart_generate_image.py`.
4. Save it with the title **"Image Generator Pro"**.
5. Go to any chat, open the tool selector (⚙️), and enable **Image Generator Pro**.

That's it! The tool will now be available for your AI to use.

---

## ⚙️ Configuration

Everything is configured through the **Open WebUI Admin panel → Settings → Images**:

- **Image Generation Engine** — Set to **ComfyUI**.
- **ComfyUI Base URL** — The address of your ComfyUI server (must be reachable from your browser).
- **ComfyUI Workflow** — Your exported workflow JSON.
- **ComfyUI Workflow Nodes** — Define which nodes receive prompt, seed, model, steps, etc.
- **Image Size** — Default dimensions (user can override).
- **Image Steps** — Default inference steps (user can override).

> 🔑 If you want seed support, make sure your workflow has a **"seed"** node configured in the Workflow Nodes section.

---

## 🗣️ How to use it (for end users)

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

## 📋 Requirements

- Open WebUI (any recent version with Tools support)
- ComfyUI server running and accessible
- Native Tool Calling enabled in Open WebUI settings
- A ComfyUI workflow exported and configured in Admin → Images

---

## ❓ FAQ

**Q: The image doesn't show up in the chat.**  
A: Make sure your `ComfyUI Base URL` is accessible from your browser. The image is served directly from ComfyUI, not stored locally.

**Q: Seed doesn't seem to do anything.**  
A: Your workflow needs a "seed" node configured in **Admin → Images → ComfyUI Workflow Nodes**. Without it, the seed value is ignored and ComfyUI uses its own random seed.

**Q: Can I still use the native image chip?**  
A: Yes! Both work independently. The chip uses the built-in Open WebUI image generation, while this tool gives you extra control.

**Q: Does it work with AUTOMATIC1111 or OpenAI?**  
A: It falls back to the original Open WebUI handler for those engines. Seed and dimension features only work with ComfyUI.
