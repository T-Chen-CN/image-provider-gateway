from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from image_provider_gateway.gateway import generate_images_batch
from image_provider_gateway.manifest import EventLogger
from image_provider_gateway.models import ImageRequest, ImageResult, QaResult
from image_provider_gateway.validation import validate_batch_requests
from image_provider_gateway import gateway as gateway_module


def _req(id_: str, **kw) -> ImageRequest:
    return ImageRequest(id=id_, prompt="hi", **kw)


def test_duplicate_output_name_rejected():
    with pytest.raises(ValueError, match="output basename must be unique"):
        validate_batch_requests([
            _req("a", output_name="same"),
            _req("b", output_name="same"),
        ])


def test_duplicate_id_as_basename_rejected():
    # id collision with someone else's output_name
    with pytest.raises(ValueError, match="output basename must be unique"):
        validate_batch_requests([
            _req("same"),
            _req("b", output_name="same"),
        ])


def test_distinct_output_names_ok():
    validate_batch_requests([_req("a", output_name="x"), _req("b", output_name="y")])


def test_qa_exception_isolated(monkeypatch, tmp_path):
    def fake_run_round(requests, output_dir, **_):
        results = []
        for r in requests:
            path = output_dir / f"{r.id}.png"
            output_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            results.append(ImageResult(
                ok=True, id=r.id, provider=r.provider, model=r.model,
                requested_size=r.size, mode=r.mode, path=str(path),
            ))
        return results

    def boom(*_a, **_k):
        raise RuntimeError("qa boom")

    monkeypatch.setattr(gateway_module, "_run_round", fake_run_round)
    monkeypatch.setattr(gateway_module, "qa_check_image", boom)

    batch = generate_images_batch(
        [_req("one")], tmp_path, api_key="k", base_url="http://x", concurrency=1, retry=0,
    )
    assert batch.results[0].ok is True
    assert batch.results[0].qa.status == "not_checked"
    assert "qa boom" in (batch.results[0].qa.qa_error or "")


def test_manifest_write_failure_survives(monkeypatch, tmp_path):
    def fake_run_round(requests, output_dir, **_):
        output_dir.mkdir(parents=True, exist_ok=True)
        return [ImageResult(
            ok=True, id=r.id, provider=r.provider, model=r.model,
            requested_size=r.size, mode=r.mode, path=str(output_dir / f"{r.id}.png"),
        ) for r in requests]

    monkeypatch.setattr(gateway_module, "_run_round", fake_run_round)

    calls = {"n": 0}
    real = gateway_module.write_json

    def flaky_write_json(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(gateway_module, "write_json", flaky_write_json)

    batch = generate_images_batch(
        [_req("one")], tmp_path, api_key="k", base_url="http://x", concurrency=1, retry=0, qa_enabled=False,
    )
    assert batch.results[0].ok is True
    assert any("disk full" in e for e in batch.write_errors)


def test_preflight_output_dir_readonly(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        with pytest.raises(OSError, match="not writable"):
            generate_images_batch(
                [_req("one")], ro, api_key="k", base_url="http://x", concurrency=1, retry=0,
            )
    finally:
        ro.chmod(0o700)


CLI = [sys.executable, "-m", "image_provider_gateway.cli"]


def _run(args, env_extra=None):
    import os
    env = os.environ.copy()
    env.setdefault("IMAGE_API_KEY", "k")
    env.setdefault("IMAGE_API_BASE_URL", "http://x")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(CLI + args, capture_output=True, text=True, env=env)


def test_cli_missing_file_structured(tmp_path):
    r = _run(["batch", "--requests", str(tmp_path / "nope.json"), "--output-dir", str(tmp_path)])
    assert r.returncode == 2
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "file_not_found"


def test_cli_invalid_json_structured(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = _run(["batch", "--requests", str(bad), "--output-dir", str(tmp_path)])
    assert r.returncode == 2
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["error"] == "invalid_requests_json"


def test_cli_top_level_not_array(tmp_path):
    bad = tmp_path / "obj.json"
    bad.write_text('{"a": 1}', encoding="utf-8")
    r = _run(["batch", "--requests", str(bad), "--output-dir", str(tmp_path)])
    assert r.returncode == 2
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["error"] == "invalid_requests_json"


def test_cli_bad_field_type(tmp_path):
    bad = tmp_path / "fields.json"
    bad.write_text(json.dumps([{"id": "a", "prompt": "p", "bogus_field": 1}]), encoding="utf-8")
    r = _run(["batch", "--requests", str(bad), "--output-dir", str(tmp_path)])
    assert r.returncode == 2
    payload = json.loads(r.stdout.strip().splitlines()[-1])
    assert payload["error"] == "invalid_request_fields"
