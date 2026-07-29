# Generate Video

Generates videos from an image (image-to-video) using Wan 2.1 or Wan 2.2 through ComfyUI. Frames follow a 4n+1 constraint imposed by the WAN temporal VAE stride. Supports single-path (Wan 2.1) and dual-path high/low resolution (Wan 2.2) pipelines.

## Model versions

### Wan 2.1

Single-path pipeline. One diffusion model, sampler, and scheduler.

| Parameter | Default |
|---|---|
| Diffusion model | Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-nvfp4.safetensors |
| Sampler | euler |
| Scheduler | simple |
| Steps | 4 |
| CFG | 1.0 |
| Model sampling shift | 5 |

### Wan 2.2

Dual-path pipeline with high and low resolution passes.

| Parameter | High path | Low path |
|---|---|---|
| Diffusion model | Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-high-nvfp4.safetensors | Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-low-nvfp4.safetensors |
| Sampler | heun | euler |
| Scheduler | simple | simple |
| Steps | 4 | 4 |
| CFG | 1.0 | 1.0 |
| Start at step | 0 | 2 |
| End at step | 2 | 10000 |
| Add noise | enable | disable |
| Return with leftover noise | enable | disable |

## Valves

### Admin

| Valve | Description |
|---|---|
| model_version | wan21 or wan22. |
| diffusion_model | JSON override for model files. Object for Wan 2.1, array for Wan 2.2. |
| lora_config | JSON array. Supports per-path LoRAs via the "path" field. |
| length | Maximum frame count (ceiling for user). -1 = no ceiling. Must be 4n+1. |
| negative_prompt | Default negative prompt. |
| comfyui_image_base_url | Override the video link base URL. |

### User

| Valve | Description |
|---|---|
| model_version | Overrides the admin valve. |
| diffusion_model | Overrides the admin valve and defaults. |
| lora_config | Overrides the admin valve. |
| length | Frame count. 0 = use admin value. Must be 4n+1. |
| negative_prompt | Overrides the admin valve or built-in default. |
| seed | -1 = random, >=0 = fixed seed. |
| steps | 4-10. Wan 2.2 rounds odd values up to the nearest even. |
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Model sources

The models listed below use mixed quantization formats (NVFP4, MXFP8) and are designed for NVIDIA Blackwell GPUs (RTX 50xx series). They require CUDA 13 and comfy-kitchen (or a compatible runtime) to load and run correctly.

Diffusion models are available on HuggingFace at [InsecureErasure](https://huggingface.co/InsecureErasure):

| File | Source |
|---|---|
| Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-nvfp4.safetensors | [InsecureErasure/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-NVFP4](https://huggingface.co/InsecureErasure/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-NVFP4) |
| Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-{high,low}-nvfp4.safetensors | [InsecureErasure/Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-NVFP4](https://huggingface.co/InsecureErasure/Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-NVFP4) |

The Wan VAE (wan_2.1_vae.safetensors) is available from the original Wan repository.

## Usage

The LLM calls this tool when the user asks to animate an image into a video. The prompt describes the desired motion in English. The image parameter accepts a filename from a previous generation or a direct URL.

The result renders as an HTML video element with autoplay, muted, and loop attributes.

## Workflow files

Place both workflow files in the tool's cache directory:

```
/app/backend/data/cache/tools/generate_video/generate_video.json
/app/backend/data/cache/tools/generate_video/generate_video_wan22.json
```
