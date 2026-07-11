# Provider Error Codes

The gateway classifies every provider failure into a stable `code` on
`result.provider_error`. Agents should switch on `code` (or `retryable`) rather
than string-matching upstream `message` text.

## Payload shape

```json
{
  "ok": false,
  "error": "http_error",
  "http_status": 400,
  "provider_error": {
    "code": "safety_violation",
    "retryable": false,
    "http_status": 400,
    "message": "Your request was rejected by the safety system.",
    "hint": "Provider safety filter rejected the prompt or reference image...",
    "provider_raw": { "error": { "code": "moderation_blocked", "message": "..." } }
  }
}
```

## Code reference

| `code` | Trigger | `retryable` | Suggested reaction |
|---|---|---|---|
| `safety_violation` | 400 with `safety`/`moderation` markers in error type/code/message | ❌ | Rewrite the prompt to drop safety-sensitive terms (nudity, minors, weapons, gore, boudoir); regenerate. |
| `content_policy_violation` | Alias for safety-family errors when providers use this literal code. | ❌ | Same as `safety_violation`. |
| `rate_limit` | HTTP 429 without a quota hint. | ✅ | Reduce concurrency and retry; consider exponential backoff. |
| `auth_failed` | HTTP 401 or 403. | ❌ | Fix the API key or the account/base URL binding; do not retry with the same credentials. |
| `quota_exceeded` | HTTP 402, or 429 with `insufficient_quota`/`quota`/`credits`/`billing` markers. | ❌ | Top up the account or switch to another provider entry. |
| `model_not_found` | HTTP 400/404 whose message references a missing/unsupported model. | ❌ | Pass a supported `--model`; run `image-provider-gateway config show <provider>` to see the configured default. |
| `bad_request` | Any other HTTP 400. | ❌ | Inspect `provider_raw`; typical culprits are unsupported `size`/`quality`/`model` combinations. |
| `server_error` | HTTP 5xx. | ✅ | Retry with backoff; provider is transient-flaky. |
| `timeout` | Local request timeout. | ✅ | Retry, or raise `--timeout` for slow models like `gpt-image-2 high`. |
| `network_error` | `URLError` before an HTTP response. | ✅ | Retry after checking connectivity/DNS to the base URL. |
| `provider_json_error` | Non-JSON response body (typically a misbehaving proxy). | ✅ | Retry once or twice; if it persists, verify the base URL and check upstream logs. |
| `unknown` | Fallback when no other rule matches. | ❌ | Inspect `provider_raw` and file an issue if a new provider variant needs classification. |

## Retry semantics

`generate_images_batch` treats `retryable` as authoritative when it is present
in `provider_error`. Legacy `RETRYABLE_ERRORS`/HTTP-status heuristics remain as
a fallback for any adapter that does not yet emit a classified payload, so
existing callers keep their old behaviour.

## Extending the classifier

`image_provider_gateway/errors.py` centralises the mapping. When you add a
provider adapter, populate `result.provider_error` with
`ClassifiedError.to_dict()` from either `classify_http_error(...)` or
`classify_transport_error(...)`. Add coverage in `tests/test_error_codes.py`
(unit-level heuristics) and, for retryability behaviour, in
`tests/test_provider_error_integration.py`.
