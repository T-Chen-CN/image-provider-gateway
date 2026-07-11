from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import traceback
from pathlib import Path

from .config import (
    CONFIG_ENV_VAR,
    DEFAULT_MODEL_FALLBACK,
    GatewayConfig,
    ProviderEntry,
    default_config_path,
    load_config,
    redact_key,
    resolve_credentials,
    save_config,
)
from .gateway import generate_image, generate_images_batch
from .manifest import dataclass_to_dict
from .models import ImageRequest


def _fail(error: str, message: str, debug: bool = False) -> int:
    payload = {"ok": False, "error": error, "message": message}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if debug:
        traceback.print_exc()
    return 2


def _ok(**payload) -> int:
    payload = {"ok": True, **payload}
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    try:
        return _main()
    except SystemExit:
        raise
    except FileNotFoundError as error:
        return _fail("file_not_found", str(error), debug=os.environ.get("IPG_DEBUG") == "1")
    except json.JSONDecodeError as error:
        return _fail("invalid_requests_json", str(error), debug=os.environ.get("IPG_DEBUG") == "1")
    except (TypeError, ValueError) as error:
        return _fail("invalid_input", str(error), debug=os.environ.get("IPG_DEBUG") == "1")
    except Exception as error:  # last-resort structured wrapper
        return _fail("internal_error", f"{type(error).__name__}: {error}", debug=os.environ.get("IPG_DEBUG") == "1")


def _load_config_safe(path: Path | None) -> tuple[GatewayConfig, str | None]:
    """Load the config file; on failure return an empty config plus warning text.

    Corrupt configs must not block explicit CLI/env credentials, so we degrade
    gracefully with a warning printed on stderr.
    """
    try:
        return load_config(path), None
    except ValueError as error:
        return GatewayConfig(), f"warning: {error}"


def _add_common_credential_args(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--base-url", default=None, help="Provider base URL (overrides IMAGE_API_BASE_URL and config)")
    subparser.add_argument("--api-key-env", default="IMAGE_API_KEY", help="Environment variable name for the API key (default IMAGE_API_KEY)")
    subparser.add_argument("--provider", default=None, help="Named provider from the config file (defaults to the file's default_provider)")
    subparser.add_argument("--config-file", default=None, help=f"Override config file path (default {default_config_path()})")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Agent-friendly image provider gateway")
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("single", help="Generate one image")
    single.add_argument("--prompt", required=True)
    single.add_argument("--id", default="01-image")
    single.add_argument("--size", default="1024x1024")
    single.add_argument("--quality", default="low")
    single.add_argument("--output-dir", required=True)
    single.add_argument("--output-name")
    single.add_argument("--model", default=None, help="Model id (defaults to provider's default_model or gpt-image-2)")
    single.add_argument("--provider-type", default="openai_images", help="Provider adapter (default openai_images)")
    single.add_argument("--mode", choices=["generate", "edit"], default="generate")
    single.add_argument("--input-image", action="append", default=[], help="Input/reference image for edit mode; can be repeated")
    single.add_argument("--qa-preset", default="basic", help="QA preset name to record/use for built-in basic technical QA")
    _add_common_credential_args(single)
    single.add_argument("--timeout", type=int, default=600, help="HTTP request timeout in seconds")
    single.add_argument("--qa", action="store_true")

    batch = subparsers.add_parser("batch", help="Generate images from a JSON request file")
    batch.add_argument("--requests", required=True, help="JSON file containing a list of image requests")
    batch.add_argument("--output-dir", required=True)
    batch.add_argument("--concurrency", type=int, default=9)
    batch.add_argument("--retry", type=int, default=2)
    _add_common_credential_args(batch)
    batch.add_argument("--timeout", type=int, default=600, help="HTTP request timeout in seconds for each image request")
    batch.add_argument("--job-id")
    batch.add_argument("--no-qa", action="store_true")

    init_parser = subparsers.add_parser("init", help="Persist a provider entry to the config file")
    init_parser.add_argument("--provider", default=None, help="Provider name (interactive prompt if omitted)")
    init_parser.add_argument("--base-url", default=None, help="Provider base URL")
    init_parser.add_argument("--api-key", default=None, help="Provider API key (avoid on shared shells; use --from-env or --api-key-stdin)")
    init_parser.add_argument("--api-key-stdin", action="store_true", help="Read API key from stdin (single line)")
    init_parser.add_argument("--default-model", default=None, help=f"Default model for this provider (default {DEFAULT_MODEL_FALLBACK})")
    init_parser.add_argument("--from-env", action="store_true", help="Read base URL and API key from IMAGE_API_BASE_URL / IMAGE_API_KEY")
    init_parser.add_argument("--set-default", action="store_true", help="Mark this provider as the default")
    init_parser.add_argument("--config-file", default=None, help=f"Override config file path (default {default_config_path()})")
    init_parser.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting when required values are missing")

    config_parser = subparsers.add_parser("config", help="Inspect or modify the persisted config")
    config_sub = config_parser.add_subparsers(dest="config_action", required=True)
    config_list = config_sub.add_parser("list", help="List configured providers")
    config_list.add_argument("--config-file", default=None)
    config_show = config_sub.add_parser("show", help="Show one provider (key is redacted by default)")
    config_show.add_argument("provider")
    config_show.add_argument("--config-file", default=None)
    config_show.add_argument("--reveal", action="store_true", help="Reveal the full API key (not recommended)")
    config_remove = config_sub.add_parser("remove", help="Remove a provider entry")
    config_remove.add_argument("provider")
    config_remove.add_argument("--config-file", default=None)
    config_set_default = config_sub.add_parser("set-default", help="Set the default provider")
    config_set_default.add_argument("provider")
    config_set_default.add_argument("--config-file", default=None)
    config_path_cmd = config_sub.add_parser("path", help="Print the resolved config file path")
    config_path_cmd.add_argument("--config-file", default=None)

    args = parser.parse_args()

    if args.command == "init":
        return _run_init(args)
    if args.command == "config":
        return _run_config(args)

    return _run_generation(args)


def _run_generation(args) -> int:
    config_path = Path(args.config_file).expanduser() if args.config_file else None
    config, config_warning = _load_config_safe(config_path)
    if config_warning:
        print(config_warning, file=sys.stderr)

    cli_api_key = None  # generation subcommands never accept a raw --api-key
    api_key, base_url, resolved_provider = resolve_credentials(
        cli_api_key=cli_api_key,
        cli_base_url=args.base_url,
        api_key_env=args.api_key_env,
        provider_name=args.provider,
        config=config,
    )

    if not api_key:
        return _fail(
            "missing_api_key",
            f"API key not found. Set {args.api_key_env} in the environment, run "
            "`image-provider-gateway init`, or pass --provider to pick a saved provider.",
        )
    if not base_url:
        return _fail(
            "missing_base_url",
            "IMAGE_API_BASE_URL is not set and no --base-url or configured provider was found. "
            "Run `image-provider-gateway init` or export IMAGE_API_BASE_URL.",
        )

    resolved_entry = config.resolve(args.provider) if config else None
    default_model = resolved_entry.default_model if resolved_entry else DEFAULT_MODEL_FALLBACK

    if args.command == "single":
        model = args.model or default_model
        request = ImageRequest(
            id=args.id,
            prompt=args.prompt,
            size=args.size,
            quality=args.quality,
            provider=args.provider_type,
            model=model,
            mode=args.mode,
            input_images=args.input_image,
            qa_preset=args.qa_preset,
            output_name=args.output_name,
        )
        result = generate_image(
            request,
            Path(args.output_dir),
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=args.timeout,
            qa_enabled=args.qa,
        )
        print(json.dumps(dataclass_to_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.ok else 1

    # batch
    request_data = json.loads(Path(args.requests).read_text(encoding="utf-8"))
    if not isinstance(request_data, list):
        return _fail("invalid_requests_json", "top-level JSON must be a list of image requests")
    try:
        requests = []
        for item in request_data:
            if not isinstance(item, dict):
                raise TypeError("each request must be an object")
            item = dict(item)
            item.setdefault("model", default_model)
            requests.append(ImageRequest(**item))
    except TypeError as error:
        return _fail("invalid_request_fields", str(error))
    result = generate_images_batch(
        requests,
        Path(args.output_dir),
        api_key=api_key,
        base_url=base_url,
        concurrency=args.concurrency,
        retry=args.retry,
        timeout_seconds=args.timeout,
        qa_enabled=not args.no_qa,
        job_id=args.job_id,
    )
    print(json.dumps(dataclass_to_dict(result), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


def _run_init(args) -> int:
    config_path = Path(args.config_file).expanduser() if args.config_file else None
    config, config_warning = _load_config_safe(config_path)
    if config_warning:
        print(config_warning, file=sys.stderr)

    interactive = not args.non_interactive and sys.stdin.isatty()

    provider_name = args.provider
    base_url = args.base_url
    api_key = args.api_key
    default_model = args.default_model

    if args.from_env:
        env_key = os.environ.get("IMAGE_API_KEY")
        env_base = os.environ.get("IMAGE_API_BASE_URL")
        api_key = api_key or env_key
        base_url = base_url or env_base
        if not env_key and not api_key:
            return _fail("missing_api_key", "IMAGE_API_KEY is not set; --from-env cannot import it")
        if not env_base and not base_url:
            return _fail("missing_base_url", "IMAGE_API_BASE_URL is not set; --from-env cannot import it")

    if args.api_key_stdin:
        stdin_key = sys.stdin.readline().strip()
        if not stdin_key:
            return _fail("missing_api_key", "--api-key-stdin was set but no key was read from stdin")
        api_key = stdin_key

    if not provider_name:
        if interactive:
            default_name = config.default_provider or "default"
            provider_name = input(f"Provider name [{default_name}]: ").strip() or default_name
        else:
            return _fail("missing_provider", "--provider is required in non-interactive mode")

    if not base_url:
        if interactive:
            existing = config.providers.get(provider_name)
            hint = f" [{existing.base_url}]" if existing else ""
            base_url = input(f"Base URL{hint}: ").strip()
            if not base_url and existing:
                base_url = existing.base_url
        if not base_url:
            return _fail("missing_base_url", "--base-url is required")

    if not api_key:
        if interactive:
            api_key = getpass.getpass("API key: ").strip()
        if not api_key:
            return _fail("missing_api_key", "--api-key, --api-key-stdin, or --from-env is required")

    if not default_model:
        if interactive:
            existing = config.providers.get(provider_name)
            default_hint = existing.default_model if existing else DEFAULT_MODEL_FALLBACK
            default_model = input(f"Default model [{default_hint}]: ").strip() or default_hint
        else:
            default_model = DEFAULT_MODEL_FALLBACK

    set_default = args.set_default
    if not set_default and interactive:
        answer = input("Set as default provider? [Y/n]: ").strip().lower()
        set_default = answer in ("", "y", "yes")

    entry = ProviderEntry(
        name=provider_name,
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
    )
    updated = config.with_provider(entry, set_default=set_default)
    written_path = save_config(updated, config_path)
    return _ok(
        action="saved",
        config_path=str(written_path),
        provider=provider_name,
        base_url=base_url,
        api_key_preview=redact_key(api_key),
        default_model=default_model,
        default_provider=updated.default_provider,
    )


def _run_config(args) -> int:
    config_path = Path(args.config_file).expanduser() if args.config_file else None
    if args.config_action == "path":
        resolved = default_config_path() if config_path is None else config_path
        return _ok(config_path=str(resolved), env_override=os.environ.get(CONFIG_ENV_VAR))

    config, config_warning = _load_config_safe(config_path)
    if config_warning:
        print(config_warning, file=sys.stderr)

    if args.config_action == "list":
        return _ok(
            config_path=str(config_path or default_config_path()),
            default_provider=config.default_provider,
            providers=[
                {
                    "name": entry.name,
                    "base_url": entry.base_url,
                    "default_model": entry.default_model,
                    "api_key_preview": redact_key(entry.api_key),
                }
                for entry in config.providers.values()
            ],
        )

    if args.config_action == "show":
        entry = config.providers.get(args.provider)
        if not entry:
            return _fail("unknown_provider", f"provider '{args.provider}' is not configured")
        payload = {
            "name": entry.name,
            "base_url": entry.base_url,
            "default_model": entry.default_model,
        }
        if args.reveal:
            payload["api_key"] = entry.api_key
        else:
            payload["api_key_preview"] = redact_key(entry.api_key)
        return _ok(**payload)

    if args.config_action == "remove":
        if args.provider not in config.providers:
            return _fail("unknown_provider", f"provider '{args.provider}' is not configured")
        updated = config.without_provider(args.provider)
        written = save_config(updated, config_path)
        return _ok(
            action="removed",
            provider=args.provider,
            config_path=str(written),
            default_provider=updated.default_provider,
        )

    if args.config_action == "set-default":
        if args.provider not in config.providers:
            return _fail("unknown_provider", f"provider '{args.provider}' is not configured")
        updated = config.with_default(args.provider)
        written = save_config(updated, config_path)
        return _ok(
            action="set-default",
            provider=args.provider,
            config_path=str(written),
        )

    return _fail("invalid_input", f"unknown config action {args.config_action!r}")


if __name__ == "__main__":
    raise SystemExit(main())
