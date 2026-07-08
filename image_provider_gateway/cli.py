from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .gateway import generate_image, generate_images_batch
from .manifest import dataclass_to_dict
from .models import ImageRequest


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent-friendly image provider gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("single", help="Generate one image")
    single.add_argument("--prompt", required=True)
    single.add_argument("--id", default="01-image")
    single.add_argument("--size", default="1024x1024")
    single.add_argument("--quality", default="low")
    single.add_argument("--output-dir", required=True)
    single.add_argument("--output-name")
    single.add_argument("--model", default="gpt-image-2")
    single.add_argument("--provider", default="openai_images")
    single.add_argument("--mode", choices=["generate", "edit"], default="generate")
    single.add_argument("--input-image", action="append", default=[], help="Input/reference image for edit mode; can be repeated")
    single.add_argument("--qa-preset", default="basic", help="QA preset name to record/use for built-in basic technical QA")
    single.add_argument("--base-url", default=None, help="Provider base URL; defaults to IMAGE_API_BASE_URL")
    single.add_argument("--api-key-env", default="IMAGE_API_KEY")
    single.add_argument("--timeout", type=int, default=600)
    single.add_argument("--qa", action="store_true")

    batch = subparsers.add_parser("batch", help="Generate images from a JSON request file")
    batch.add_argument("--requests", required=True, help="JSON file containing a list of image requests")
    batch.add_argument("--output-dir", required=True)
    batch.add_argument("--concurrency", type=int, default=9)
    batch.add_argument("--retry", type=int, default=2)
    batch.add_argument("--base-url", default=None, help="Provider base URL; defaults to IMAGE_API_BASE_URL")
    batch.add_argument("--api-key-env", default="IMAGE_API_KEY")
    batch.add_argument("--timeout", type=int, default=600)
    batch.add_argument("--job-id")
    batch.add_argument("--no-qa", action="store_true")

    args = parser.parse_args()
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(json.dumps({"ok": False, "error": f"{args.api_key_env} is required"}, ensure_ascii=False), file=sys.stderr)
        return 2

    if args.command == "single":
        request = ImageRequest(
            id=args.id,
            prompt=args.prompt,
            size=args.size,
            quality=args.quality,
            provider=args.provider,
            model=args.model,
            mode=args.mode,
            input_images=args.input_image,
            qa_preset=args.qa_preset,
            output_name=args.output_name,
        )
        result = generate_image(
            request,
            Path(args.output_dir),
            api_key=api_key,
            base_url=args.base_url,
            timeout_seconds=args.timeout,
            qa_enabled=args.qa,
        )
        print(json.dumps(dataclass_to_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    request_data = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    requests = [ImageRequest(**item) for item in request_data]
    result = generate_images_batch(
        requests,
        Path(args.output_dir),
        api_key=api_key,
        base_url=args.base_url,
        concurrency=args.concurrency,
        retry=args.retry,
        timeout_seconds=args.timeout,
        qa_enabled=not args.no_qa,
        job_id=args.job_id,
    )
    print(json.dumps(dataclass_to_dict(result), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
