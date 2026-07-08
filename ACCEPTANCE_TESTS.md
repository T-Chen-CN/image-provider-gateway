# Acceptance Test Standard

This document defines how to accept `image-provider-gateway` before Agents rely on it.

## Scope

The tool is accepted when it can:

1. Generate one text-to-image result from a normalized request.
2. Generate one image-to-image/edit result from a normalized request with `input_images`.
3. Save the model output exactly as returned, without resize/crop/format conversion.
4. Run mixed text-to-image and image-to-image batch requests with configurable concurrency up to 9.
5. Retry generation/edit failures without rerunning successful images.
6. Write `manifest.json` and `events.jsonl` with enough data for delivery and debugging.
7. Run non-blocking basic technical QA and report warnings without automatic quality retries.
8. Keep business/visual QA in external preset files, not hardcoded gateway logic.
9. Avoid exposing provider host names in Agent-facing request schemas.

## Non-Paid Local Tests

Run from `tools/image-provider-gateway`:

```bash
python3 -m compileall image_provider_gateway
PYTHONPATH=. python3 -m image_provider_gateway.cli batch \
  --requests config.example.json \
  --output-dir /tmp/image-provider-gateway-no-key
```

Expected:

- Compile succeeds.
- No-key CLI exits non-zero.
- Error message says `IMAGE_API_KEY is required`.
- No API request is sent.

## Single Text-to-Image Paid Smoke Test

Requires `IMAGE_API_KEY` and `IMAGE_API_BASE_URL`.

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --id 01-main \
  --prompt "A clean ecommerce product photo of a white ceramic mug. No text." \
  --size 1024x1024 \
  --quality low \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs/smoke-single/images \
  --qa
```

Pass criteria:

- Exit code `0`.
- Result JSON has `ok: true` and `mode: generate`.
- `path` points to an existing image.
- `format_detected` is not `UNKNOWN`.
- `actual_size` is populated.
- No resizing/cropping/conversion happens after provider return.
- `qa.status` is `pass`, `warning`, or `severe_warning`; QA never flips `ok` to false.

## Single Image-to-Image Paid Smoke Test

Use a known valid local image path, preferably a prior text-to-image output.

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli single \
  --id 02-reference-scene \
  --mode edit \
  --input-image /absolute/path/to/reference-product.png \
  --prompt "Keep the input product identity unchanged. Put it on a clean breakfast table. No text." \
  --size 1024x1024 \
  --quality low \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs/smoke-edit/images \
  --qa
```

Pass criteria:

- Exit code `0`, unless the provider explicitly reports that `/images/edits` is unsupported.
- Result JSON has `ok: true` and `mode: edit`.
- `path` points to an existing image.
- `format_detected` is not `UNKNOWN`.
- `actual_size` is populated.
- Provider errors are captured in `provider_error` if unsupported.
- QA only warns; it does not trigger a retry.

## Batch Paid Smoke Test

Prepare a 9-task JSON file that can mix `mode: generate` and `mode: edit`.

```bash
PYTHONPATH=. python3 -m image_provider_gateway.cli batch \
  --requests requests-9.json \
  --base-url "$IMAGE_API_BASE_URL" \
  --output-dir outputs \
  --concurrency 9 \
  --retry 2
```

Pass criteria:

- First round starts with `concurrency=9` when 9 requests exist.
- Text-to-image and image-to-image tasks both run through the same batch pipeline.
- Successful images are saved under `outputs/<job_id>/images/`.
- `manifest.json` exists and contains ordered `results` matching request IDs.
- `events.jsonl` exists and includes `batch_started`, per-image success/failure events, `round_completed`, `partial_delivery_ready`, and `batch_completed`.
- Failures include `error`, `http_status` where available, and `provider_error` when provider JSON/body exists.
- Successful images are not regenerated in retry rounds.

## Failure Retry Test

Use one valid request and one intentionally invalid request, for example an unsupported provider, intentionally bad model, or edit request with a missing input image in a separate test copy.

Pass criteria:

- Valid image result remains successful and is not rerun.
- Invalid task records failure reason.
- Retry attempts are recorded in `attempts` where retryable.
- Final manifest preserves both success and failure results.

## QA Preset Acceptance

Required preset files:

- `qa_presets/basic.json` for gateway-level technical checks.
- `qa_presets/ecommerce_product.json` for ecommerce product listing visual QA guidance.

Pass criteria:

- `basic.json` contains only provider-agnostic technical checks: output path, decodable image, actual size, aspect ratio.
- `ecommerce_product.json` contains business visual checks such as product identity, deformation, text quality, image slot fit, platform risk, and visual polish.
- `ImageRequest.qa_preset` defaults to `basic`.
- Result `qa.preset` records which preset was used or requested.
- Gateway built-in QA does not hardcode ecommerce-specific checks.
- QA warning events may appear as `qa_warning`.
- QA warnings never trigger automatic retries.
- Agent wording asks the user to confirm manual retry, e.g. `重试5图`.

## Delivery Acceptance

Agent behavior on a 9-image job:

1. Start 9 concurrent generation/edit tasks.
2. When first round completes, send all successful images immediately.
3. If generation/edit failures exist, tell the user which image IDs failed and that only those are retrying.
4. If QA warnings exist, send the images anyway and mention likely issues.
5. If the user replies `重试X图`, queue that user-requested retry after current generation/retry tasks finish.

## Rejection Conditions

Reject the tool if it:

- Stores API keys in manifests, events, README examples, or request JSON.
- Resizes, crops, or converts provider images by default.
- Automatically retries images solely because QA warns about quality.
- Drops successful images because other images failed.
- Requires Agents to know provider-specific response parsing or provider host names.
- Treats image-to-image as a separate second-class flow without batch/retry/QA support.
- Hardcodes ecommerce visual QA rules directly into gateway source instead of `qa_presets/ecommerce_product.json`.
