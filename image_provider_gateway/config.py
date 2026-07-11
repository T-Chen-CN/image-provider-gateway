"""Persistent provider configuration for image-provider-gateway.

Layered precedence (highest to lowest):
1. Explicit CLI arguments (``--base-url``, ``--api-key`` via ``--api-key-env``)
2. Environment variables (``IMAGE_API_KEY``, ``IMAGE_API_BASE_URL``)
3. Configuration file (``~/.config/image-provider-gateway/config.json``)

The configuration file is only a convenience for interactive users; it is never
required.  Callers may keep using environment variables exclusively.

The file is stored with ``chmod 600`` because it contains API keys.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


CONFIG_ENV_VAR = "IMAGE_PROVIDER_GATEWAY_CONFIG"
DEFAULT_MODEL_FALLBACK = "gpt-image-2"


@dataclass
class ProviderEntry:
    name: str
    base_url: str
    api_key: str
    default_model: str = DEFAULT_MODEL_FALLBACK

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "api_key": self.api_key,
            "default_model": self.default_model,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "ProviderEntry":
        if not isinstance(data, dict):
            raise ValueError(f"provider '{name}' entry must be an object")
        base_url = data.get("base_url")
        api_key = data.get("api_key")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"provider '{name}' missing base_url")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError(f"provider '{name}' missing api_key")
        default_model = data.get("default_model") or DEFAULT_MODEL_FALLBACK
        if not isinstance(default_model, str) or not default_model:
            raise ValueError(f"provider '{name}' default_model must be a non-empty string")
        return cls(name=name, base_url=base_url, api_key=api_key, default_model=default_model)


@dataclass
class GatewayConfig:
    default_provider: str | None = None
    providers: dict[str, ProviderEntry] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_provider": self.default_provider,
            "providers": {name: entry.to_dict() for name, entry in self.providers.items()},
        }

    def with_provider(self, entry: ProviderEntry, *, set_default: bool = False) -> "GatewayConfig":
        providers = dict(self.providers)
        providers[entry.name] = entry
        default_provider = self.default_provider
        if set_default or default_provider is None:
            default_provider = entry.name
        return replace(self, default_provider=default_provider, providers=providers)

    def without_provider(self, name: str) -> "GatewayConfig":
        if name not in self.providers:
            raise KeyError(name)
        providers = {existing: entry for existing, entry in self.providers.items() if existing != name}
        default_provider = None if self.default_provider == name else self.default_provider
        if default_provider is None and providers:
            default_provider = next(iter(providers))
        return replace(self, default_provider=default_provider, providers=providers)

    def with_default(self, name: str) -> "GatewayConfig":
        if name not in self.providers:
            raise KeyError(name)
        return replace(self, default_provider=name)

    def resolve(self, provider: str | None) -> ProviderEntry | None:
        name = provider or self.default_provider
        if not name:
            return None
        return self.providers.get(name)


def default_config_path() -> Path:
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "image-provider-gateway" / "config.json"


def load_config(path: Path | None = None) -> GatewayConfig:
    """Load a config file if it exists; return an empty config otherwise.

    Corrupt/malformed files raise ``ValueError`` so callers can decide how to
    surface the issue to the user.  Missing files are not an error.
    """
    target = path or default_config_path()
    if not target.exists():
        return GatewayConfig()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"failed to read config file {target}: {error}") from error
    if not raw.strip():
        return GatewayConfig()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"config file {target} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"config file {target} top-level must be an object")
    default_provider = data.get("default_provider")
    if default_provider is not None and not isinstance(default_provider, str):
        raise ValueError("default_provider must be a string or null")
    raw_providers = data.get("providers") or {}
    if not isinstance(raw_providers, dict):
        raise ValueError("providers must be an object")
    providers: dict[str, ProviderEntry] = {}
    for name, entry in raw_providers.items():
        if not isinstance(name, str) or not name:
            raise ValueError("provider names must be non-empty strings")
        providers[name] = ProviderEntry.from_dict(name, entry)
    if default_provider is not None and default_provider not in providers:
        # Silently drop dangling default; callers can still resolve by name.
        default_provider = next(iter(providers)) if providers else None
    return GatewayConfig(default_provider=default_provider, providers=providers)


def save_config(config: GatewayConfig, path: Path | None = None) -> Path:
    """Persist config atomically with ``chmod 600``.

    Returns the resolved path so callers can display it to the user.
    """
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(stat.S_IRWXU)
    except OSError:
        # Directory may already have appropriate permissions or be on a filesystem
        # that does not support chmod; ignore silently.
        pass
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def redact_key(api_key: str) -> str:
    """Return a display-safe key preview: keeps first/last 4 chars, masks middle."""
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}…{api_key[-4:]}"


def resolve_credentials(
    *,
    cli_api_key: str | None,
    cli_base_url: str | None,
    api_key_env: str,
    provider_name: str | None,
    config: GatewayConfig | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve (api_key, base_url, resolved_provider_name) using the precedence chain.

    Precedence per field: CLI arg > environment variable > config file entry.

    ``provider_name`` chooses which config entry to consult; when ``None``, the
    configured default provider is used.
    """
    entry = config.resolve(provider_name) if config else None
    resolved_provider = entry.name if entry else provider_name
    api_key = cli_api_key or os.environ.get(api_key_env) or (entry.api_key if entry else None)
    base_url = cli_base_url or os.environ.get("IMAGE_API_BASE_URL") or (entry.base_url if entry else None)
    return api_key, base_url, resolved_provider
