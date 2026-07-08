# image-provider-gateway

A small Agent-facing image generation gateway. Agents submit normalized image tasks; this tool handles provider calls, concurrent batch generation, retries, image saving, manifest/events, and non-blocking QA warnings.

## Provider Contract

The first implementation targets OpenAI-compatible Images APIs:

- Text-to-image endpoint: `POST {IMAGE_API_BASE_URL}/images/generations`
- Image-to-image / edit endpoint: `POST {IMAGE_API_BASE_URL}/images/edits`
- Model default: `gpt-image-2`
- Response image field: `data[0].b64_json`

Provider-specific host names should live in environment/config, not in Agent prompts or request schemas.

Observed behavior from the tested OpenAI-compatible provider:

- `size` controls aspect ratio / generation tier, not exact final pixels.
- `format` is not required and is not relied on.
- Output images are delivered as generated. The tool does not resize, crop, or convert images.

## Environment

```bash
export IMAGE_API_KEY="<your-provider-api-key>"
export IMAGE_API_BASE_URL="<your-provider-base-url>"
```

Never store API keys in request JSON, manifests, or events.

## Single Text-to-Image

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --id 01-main \
  --prompt "A clean ecommerce product photo of a white ceramic mug. No text." \
  --size 1024x1024 \
  --quality low \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs/manual-test/images \
  --qa
```

## Single Image-to-Image / Edit

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --id 02-reference-scene \
  --mode edit \
  --input-image /absolute/path/to/reference-product.png \
  --prompt "Keep the input product identity unchanged. Place it in a clean breakfast table scene. No text." \
  --size 1024x1024 \
  --quality low \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs/manual-test/images \
  --qa
```

## Batch Generation

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli batch \
  --requests config.example.json \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs \
  --concurrency 9 \
  --retry 2
```

Batch requests can mix `mode: "generate"` and `mode: "edit"` tasks. Image-to-image tasks use `input_images`.

Batch behavior:

1. First round runs up to `concurrency` image tasks in parallel.
2. Each image succeeds or fails independently.
3. First-round successes are recorded via `partial_delivery_ready`; the Agent should deliver those images immediately.
4. Failed generation/edit tasks are retried automatically up to `retry` times.
5. Retry successes are appended and can be delivered later.
6. Final failures remain in `manifest.json` with HTTP/provider/content error details.
7. QA warnings never trigger automatic retries.

## Request Schema

```json
{
  "id": "01-main",
  "prompt": "...",
  "size": "1024x1024",
  "quality": "low",
  "provider": "openai_images",
  "model": "gpt-image-2",
  "mode": "generate",
  "input_images": [],
  "output_name": "01-main",
  "metadata": {}
}
```

For image-to-image:

```json
{
  "id": "02-reference-scene",
  "prompt": "Keep the input product identity unchanged...",
  "size": "1024x1024",
  "quality": "low",
  "provider": "openai_images",
  "model": "gpt-image-2",
  "mode": "edit",
  "input_images": ["/absolute/path/to/reference-product.png"],
  "output_name": "02-reference-scene"
}
```

Recommended `size` values for the tested provider family:

- `1024x1024`
- `1536x1024`
- `1024x1536`
- `2048x2048`
- `2048x1152`
- `3840x2160`
- `2160x3840`
- `auto`

Custom sizes should follow provider rules when available: max side <= 3840px, both sides multiples of 16px, long/short ratio <= 3:1, total pixels between 655360 and 8294400.

## Output Structure

```text
outputs/<job_id>/
  images/
    01-main.png
    02-reference-scene.png
  manifest.json
  events.jsonl
```

`manifest.json` records requests, ordered results, retry pending IDs, and all attempts. `events.jsonl` records stage events such as `batch_started`, `image_succeeded`, `image_failed`, `partial_delivery_ready`, `retry_started`, `qa_warning`, and `batch_completed`.

## QA Presets

QA is split into generic gateway checks and optional business presets. Presets live in `qa_presets/` so upper-layer Skills can select or extend them without hardcoding business rules into the gateway.

Available presets:

- `qa_presets/basic.json`: generic technical QA for file existence, decodability, detected size, and aspect ratio. This is the gateway default.
- `qa_presets/ecommerce_product.json`: ecommerce product listing QA checklist for product identity, deformation, text, slot fit, platform risk, and visual polish. This is intended for upper-layer Skills or vision QA providers.

Request field:

```json
{
  "qa_preset": "basic"
}
```

For ecommerce generation, upper-layer Skills can request:

```json
{
  "qa_preset": "ecommerce_product"
}
```

Important boundary:

- The gateway built-in QA only performs basic technical checks.
- Business/visual QA should read preset files and run in an upper-layer Skill or external QA provider.
- QA warnings never trigger automatic retries. Only generation/edit failures are retried automatically.
- If visual QA flags an image, the Agent should deliver the image with a warning and ask whether to regenerate specific image IDs.

Generation/edit failures that can auto-retry:

- HTTP timeout / `408` / `429` / `5xx`
- Provider JSON error
- Missing `b64_json`
- Invalid or unparseable image bytes
- Missing input image for edit tasks

Ecommerce visual risks covered by the preset:

- Product identity mismatch
- Product deformation
- Main subject clarity
- Image slot fit: main, lifestyle, feature, detail, SKU
- Text/typography errors
- Unsupported visual claims
- Human/body/hand deformation
- Scale plausibility
- Background/prop confusion
- Platform policy risk
- Visual polish

Recommended Agent wording:

```text
已生成 7/9 张，先发成功图。
第 3、8 张生成失败：timeout，正在自动重试。
第 5 图可能存在文案错字，第 7 图可能有人物手部变形。建议回复“重试5图”或“重试5、7图”。
所有当前生图任务完成后，我会按你的确认重新生成对应图片。
```
