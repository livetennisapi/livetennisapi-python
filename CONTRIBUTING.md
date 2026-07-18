# Contributing

Thanks for helping improve the client.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not contract"     # unit tests, no API key needed
```

## Contract tests

The unit tests use a mocked transport. The **contract** tests run against the
real API and are what prove the models match production:

```bash
LIVETENNISAPI_KEY=twjp_… pytest -m contract
```

They skip automatically without a key, and tolerate an empty slate — there may
genuinely be no live matches at 3am.

## Before opening a PR

```bash
ruff check src/ tests/ && ruff format --check src/ && pytest -m "not contract"
```

## Reporting a spec mismatch

If the API returns something these models don't expect, that's the most
valuable bug report there is. Include the endpoint, the request, and the raw
response (`obj.raw`). The [spec](https://github.com/livetennisapi/openapi) is
the source of truth; if the spec and the API disagree, the spec gets fixed.

## Design rules

Two constraints are not up for negotiation, because the API's contract depends
on them:

1. **Never reject an unknown field.** Additive changes ship within `v1`.
2. **Never retry a non-429 4xx.** A bad key or an unentitled tier cannot start
   working, and retrying only burns the caller's rate limit.
