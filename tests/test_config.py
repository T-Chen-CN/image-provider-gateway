"""Tests for the persistent config module and its CLI integration."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from image_provider_gateway.config import (
    GatewayConfig,
    ProviderEntry,
    default_config_path,
    load_config,
    redact_key,
    resolve_credentials,
    save_config,
)


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    base_env = {**os.environ}
    if env is not None:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "image_provider_gateway.cli", *args],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_load_missing_config_returns_empty(tmp_path):
    config = load_config(tmp_path / "missing.json")
    assert config.default_provider is None
    assert config.providers == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    entry = ProviderEntry(name="yunwu", base_url="https://yunwu.example/v1", api_key="sk-abcd1234")
    config = GatewayConfig().with_provider(entry, set_default=True)
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.default_provider == "yunwu"
    assert loaded.providers["yunwu"].base_url == "https://yunwu.example/v1"
    assert loaded.providers["yunwu"].api_key == "sk-abcd1234"


def test_save_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "sub" / "config.json"
    entry = ProviderEntry(name="p", base_url="https://x", api_key="sk-secret-key")
    save_config(GatewayConfig().with_provider(entry, set_default=True), path)
    mode = stat.S_IMODE(path.stat().st_mode)
    # Owner read+write only; group/other must not have any bits set.
    assert mode & (stat.S_IRUSR | stat.S_IWUSR) == mode


def test_load_rejects_corrupt_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not: json ", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_load_rejects_missing_fields(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"providers": {"p": {"base_url": "u"}}}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_resolve_credentials_precedence(tmp_path, monkeypatch):
    entry = ProviderEntry(name="yunwu", base_url="https://file-url", api_key="file-key")
    config = GatewayConfig().with_provider(entry, set_default=True)

    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_API_BASE_URL", raising=False)

    # 1. Config only
    key, url, _ = resolve_credentials(
        cli_api_key=None, cli_base_url=None, api_key_env="IMAGE_API_KEY",
        provider_name=None, config=config,
    )
    assert (key, url) == ("file-key", "https://file-url")

    # 2. Env beats config
    monkeypatch.setenv("IMAGE_API_KEY", "env-key")
    monkeypatch.setenv("IMAGE_API_BASE_URL", "https://env-url")
    key, url, _ = resolve_credentials(
        cli_api_key=None, cli_base_url=None, api_key_env="IMAGE_API_KEY",
        provider_name=None, config=config,
    )
    assert (key, url) == ("env-key", "https://env-url")

    # 3. CLI beats env
    key, url, _ = resolve_credentials(
        cli_api_key="cli-key", cli_base_url="https://cli-url", api_key_env="IMAGE_API_KEY",
        provider_name=None, config=config,
    )
    assert (key, url) == ("cli-key", "https://cli-url")


def test_resolve_credentials_no_config_returns_none(monkeypatch):
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_API_BASE_URL", raising=False)
    key, url, _ = resolve_credentials(
        cli_api_key=None, cli_base_url=None, api_key_env="IMAGE_API_KEY",
        provider_name=None, config=None,
    )
    assert key is None
    assert url is None


def test_redact_key_hides_middle():
    assert redact_key("") == ""
    assert redact_key("sk-abc") == "******"
    assert redact_key("sk-ABCDEFGHIJKL") == "sk-A…IJKL"


def test_gateway_config_with_default_switches_and_without_provider_promotes(tmp_path):
    a = ProviderEntry(name="a", base_url="u", api_key="k1")
    b = ProviderEntry(name="b", base_url="u", api_key="k2")
    config = GatewayConfig().with_provider(a, set_default=True).with_provider(b)
    assert config.default_provider == "a"
    switched = config.with_default("b")
    assert switched.default_provider == "b"
    dropped = switched.without_provider("b")
    assert dropped.default_provider == "a"
    assert "b" not in dropped.providers


def test_default_config_path_env_override(monkeypatch, tmp_path):
    override = tmp_path / "custom.json"
    monkeypatch.setenv("IMAGE_PROVIDER_GATEWAY_CONFIG", str(override))
    assert default_config_path() == override


# ---------- CLI integration ----------


def test_cli_init_non_interactive_writes_config(tmp_path):
    config_path = tmp_path / "config.json"
    proc = _run_cli(
        "init",
        "--provider", "yunwu",
        "--base-url", "https://yunwu.example/v1",
        "--api-key", "sk-test-key",
        "--default-model", "gpt-image-2",
        "--set-default",
        "--non-interactive",
        "--config-file", str(config_path),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "yunwu"
    assert payload["api_key_preview"] == "sk-t…-key"
    assert config_path.exists()
    loaded = load_config(config_path)
    assert loaded.default_provider == "yunwu"
    assert loaded.providers["yunwu"].api_key == "sk-test-key"


def test_cli_init_from_env(tmp_path):
    config_path = tmp_path / "config.json"
    proc = _run_cli(
        "init",
        "--provider", "p",
        "--from-env",
        "--set-default",
        "--non-interactive",
        "--config-file", str(config_path),
        env={"IMAGE_API_KEY": "env-key-value", "IMAGE_API_BASE_URL": "https://env.example/v1"},
    )
    assert proc.returncode == 0, proc.stderr
    loaded = load_config(config_path)
    assert loaded.providers["p"].api_key == "env-key-value"
    assert loaded.providers["p"].base_url == "https://env.example/v1"


def test_cli_init_non_interactive_missing_provider_fails(tmp_path):
    proc = _run_cli(
        "init",
        "--base-url", "https://x",
        "--api-key", "k",
        "--non-interactive",
        "--config-file", str(tmp_path / "cfg.json"),
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"] == "missing_provider"


def test_cli_config_list_and_show_and_remove(tmp_path):
    config_path = tmp_path / "config.json"
    _run_cli(
        "init", "--provider", "one", "--base-url", "https://one", "--api-key", "aaaa-bbbb-cccc",
        "--set-default", "--non-interactive", "--config-file", str(config_path),
    )
    _run_cli(
        "init", "--provider", "two", "--base-url", "https://two", "--api-key", "xxxx-yyyy-zzzz",
        "--non-interactive", "--config-file", str(config_path),
    )
    listed = json.loads(_run_cli("config", "list", "--config-file", str(config_path)).stdout)
    assert listed["default_provider"] == "one"
    assert {p["name"] for p in listed["providers"]} == {"one", "two"}
    # Preview redacts keys.
    previews = {p["name"]: p["api_key_preview"] for p in listed["providers"]}
    assert previews["one"] == "aaaa…cccc"

    shown = json.loads(_run_cli("config", "show", "one", "--config-file", str(config_path)).stdout)
    assert "api_key" not in shown
    revealed = json.loads(_run_cli("config", "show", "one", "--reveal", "--config-file", str(config_path)).stdout)
    assert revealed["api_key"] == "aaaa-bbbb-cccc"

    default_switched = json.loads(_run_cli("config", "set-default", "two", "--config-file", str(config_path)).stdout)
    assert default_switched["provider"] == "two"

    removed = json.loads(_run_cli("config", "remove", "two", "--config-file", str(config_path)).stdout)
    assert removed["ok"] is True
    remaining = load_config(config_path)
    assert set(remaining.providers.keys()) == {"one"}
    # After removing the current default, the remaining provider is auto-promoted.
    assert remaining.default_provider == "one"


def test_cli_config_show_unknown_provider(tmp_path):
    config_path = tmp_path / "config.json"
    _run_cli(
        "init", "--provider", "one", "--base-url", "https://one", "--api-key", "k",
        "--set-default", "--non-interactive", "--config-file", str(config_path),
    )
    proc = _run_cli("config", "show", "missing", "--config-file", str(config_path))
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["error"] == "unknown_provider"


def test_cli_generation_reads_config(tmp_path, monkeypatch):
    """Ensure `single` can source credentials from the config file when env vars are absent."""
    config_path = tmp_path / "config.json"
    _run_cli(
        "init", "--provider", "yunwu", "--base-url", "https://yunwu.example/v1",
        "--api-key", "sk-abc", "--set-default", "--non-interactive",
        "--config-file", str(config_path),
    )
    # Provide neither env nor CLI credentials; the CLI must pick them from the config file.
    proc = _run_cli(
        "single",
        "--prompt", "test",
        "--output-dir", str(tmp_path / "out"),
        "--config-file", str(config_path),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "IMAGE_PROVIDER_GATEWAY_CONFIG": str(config_path),
        },
    )
    # We expect exit code 1 (generation failure due to unreachable URL) NOT 2 (missing creds).
    # The important thing is that the CLI did NOT fail with missing_api_key/missing_base_url.
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert payload.get("error") not in {"missing_api_key", "missing_base_url"}
