# Generate Caption

Generates a caption for an image using Florence-2 via ComfyUI. Supports five Florence-2 model variants and multiple caption tasks. The caption is always returned in English for accuracy; the LLM translates it when replying in the user's language.

## Models

| Value | Label |
|---|---|
| Florence-2-base-ft | Default. Fine-tuned base model. |
| Florence-2-Flux-Large | Larger variant. |
| Florence-2-large-interleaved | Interleaved training variant. |
| Florence-2-large-nsfw-pt | NSFW-capable variant. |

## Tasks

| Value | Description |
|---|---|
| caption | Standard caption. |
| detailed_caption | More descriptive caption (default). |
| more_detailed_caption | Verbose description. |
| nsfw_caption | Caption without safety filtering. |
| nsfw_detailed_caption | Detailed caption without safety filtering. |

## Valves

### Admin

| Valve | Description |
|---|---|
| model | Default Florence-2 model. |
| task | Default caption task. |
| max_new_tokens | Ceiling for user token values. 0 = no ceiling. |
| max_num_beams | Ceiling for user beam values (1-10). |

### User

| Valve | Description |
|---|---|
| model | Overrides the admin valve. |
| task | Overrides the admin valve. |
| new_tokens | 0 = use admin ceiling, 1-4096 = explicit value. |
| num_beams | Beam search width. Empty = use admin ceiling. |
| do_sample | False = greedy decoding, True = sampled decoding. |
| seed | -1 = random, >=1 = fixed seed. |

## Usage

The LLM calls this tool automatically when it needs to interpret image content. The detail_level parameter should only be passed when the user explicitly asks for shorter or more detailed descriptions.

## Workflow file

Place `generate_caption.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/generate_caption/generate_caption.json
```
