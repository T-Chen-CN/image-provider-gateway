# Image Provider Gateway

A small Agent-friendly gateway for OpenAI-compatible image generation APIs.

It gives an Agent one stable interface for single-image generation, image-to-image/edit tasks, and 9-image ecommerce batches, while keeping provider credentials and runtime endpoints outside the repository.

## Why This Exists

Ecommerce image production usually needs more than a one-off image API call:

- generate or edit multiple images in parallel
- keep product-reference images attached to edit tasks
- retry failed image jobs without rerunning successful ones
- deliver successful images first while failed items retry
- record manifests and event logs for traceability
- separate technical QA from business/visual QA

This gateway handles that execution layer so higher-level AgentSkills can focus on product strategy, prompts, QA rules, and user delivery.

## Core Features

- Text-to-image via OpenAI-compatible Images API generation endpoint.
- Image-to-image/edit via OpenAI-compatible Images API edit endpoint.
- Single-image and batch execution.
- Batch concurrency, including `concurrency=9` for ecommerce 9-image sets.
- Selective retry for failed generation/edit jobs.
- Stable output paths, `manifest.json`, and `events.jsonl`.
- Basic technical QA for saved image metadata and aspect ratio checks.
- Pluggable QA preset files for business-specific review guidance.
- No committed API keys, provider base URLs, generated images, or local secrets.

## Repository Layout

```text
image_provider_gateway/
  cli.py
  gateway.py
  image_probe.py
  manifest.py
  models.py
  qa_check.py
  providers/
    openai_images.py
qa_presets/
  basic.json
  ecommerce_product.json
tests/
  test_batch_retry.py
config.example.json
ACCEPTANCE_TESTS.md
```

## Runtime Configuration

The gateway reads credentials and provider endpoint configuration at runtime.

```bash
export IMAGE_API_KEY="<your-provider-api-key>"
export IMAGE_API_BASE_URL="<your-provider-base-url>"
```

Do not commit real values. The repository intentionally contains no real provider URL or API key.

## Request Model

Each image request uses a provider-neutral schema:

```json
{
  "id": "01-main",
  "prompt": "Create a clean ecommerce hero image...",
  "size": "1024x1024",
  "quality": "low",
  "provider": "openai_images",
  "model": "gpt-image-2",
  "mode": "edit",
  "input_images": ["/path/to/reference-product.png"],
  "qa_preset": "ecommerce_product",
  "output_name": "01-main",
  "metadata": {
    "slot": "main"
  }
}
```

`mode` can be:

- `generate`: text-to-image.
- `edit`: image-to-image/edit with one or more `input_images`.

## CLI Usage

Run a single text-to-image request:

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --prompt "Create a clean ecommerce product image" \
  --output-dir outputs/demo-single \
  --output-name 01-main
```

Run an image-to-image/edit request:

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --mode edit \
  --input-image /path/to/reference-product.png \
  --prompt "Preserve the exact product and place it in a bright kitchen scene" \
  --output-dir outputs/demo-edit \
  --output-name 02-lifestyle \
  --qa-preset ecommerce_product
```

Run a batch from JSON:

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli batch \
  --requests config.example.json \
  --output-dir outputs/demo-batch \
  --concurrency 9 \
  --retry 2
```

You can also pass `--base-url` and `--api-key-env`, but environment variables are recommended for normal use.

Supported `single` CLI options: `--prompt`, `--id`, `--size`, `--quality`, `--output-dir`, `--output-name`, `--model`, `--provider`, `--mode`, `--input-image`, `--qa-preset`, `--base-url`, `--api-key-env`, `--timeout`, and `--qa`.

Supported `batch` CLI options: `--requests`, `--output-dir`, `--concurrency`, `--retry`, `--base-url`, `--api-key-env`, `--timeout`, `--job-id`, and `--no-qa`.

## Output Layout

A batch run creates a job directory:

```text
outputs/<job_id>/
  images/
    01-main.png
    02-lifestyle.png
  manifest.json
  events.jsonl
```

`manifest.json` stores final request/result metadata. `events.jsonl` records execution events such as:

- `batch_started`
- `image_succeeded`
- `image_failed`
- `partial_delivery_ready`
- `retry_started`
- `qa_warning`
- `batch_completed`

## Retry Policy

Automatic retry applies to technical generation/edit failures, such as:

- timeout
- HTTP 408 / 429 / 5xx
- provider JSON error
- missing `b64_json`
- invalid image bytes
- missing edit input image

Successful images are not rerun. Visual QA warnings do not trigger automatic retries.

## QA Presets

Built-in gateway QA is intentionally generic and technical. Business rules live in separate preset files:

- `qa_presets/basic.json`: output file, decodable image, detected size, aspect ratio.
- `qa_presets/ecommerce_product.json`: product identity, deformation, slot fit, claims, text, props, and platform risk.

Higher-level AgentSkills can read a preset and run their own vision QA process. The gateway records the selected `qa_preset` but does not hardcode ecommerce business logic.

## Validation

Compile the package:

```bash
python3 -m compileall image_provider_gateway
```

Validate example JSON:

```bash
python3 -m json.tool config.example.json >/dev/null
python3 -m json.tool qa_presets/basic.json >/dev/null
python3 -m json.tool qa_presets/ecommerce_product.json >/dev/null
```

Run the mock retry test with the standard library or adapt it to your test runner.

See `ACCEPTANCE_TESTS.md` for expected behavior.

## Security Notes

- Do not commit `.env` files.
- Do not commit generated images unless they are intentional examples.
- Do not commit real provider endpoints if they are private infrastructure.
- Do not put API keys or secret file paths into manifests or request JSON.
