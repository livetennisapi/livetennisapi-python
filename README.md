<div align="center">

<img src="https://raw.githubusercontent.com/livetennisapi/.github/main/profile/banner.jpg" alt="Live Tennis API" width="640">

# livetennisapi

**Official Python client for the [Live Tennis API](https://livetennisapi.com).**

Real-time tennis scores, players, rankings, match-winner market prices and model
win-probability — for ATP, WTA, Challenger and ITF, over REST and WebSocket.

[![PyPI](https://img.shields.io/pypi/v/livetennisapi.svg)](https://pypi.org/project/livetennisapi/)
[![Python](https://img.shields.io/pypi/pyversions/livetennisapi.svg)](https://pypi.org/project/livetennisapi/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[**Documentation**](https://docs.livetennisapi.com) · [**Get a free API key**](https://livetennisapi.com/subscribe/free)

</div>

---

## Install

```bash
pip install livetennisapi          # REST client + CLI
pip install "livetennisapi[all]"   # + WebSocket feed and rich CLI tables
```

## Use

```python
from livetennisapi import LiveTennisAPI

with LiveTennisAPI(api_key="twjp_…") as client:   # or set LIVETENNISAPI_KEY
    for match in client.list_matches(status="live"):
        print(match.tournament, match.p1.name, "vs", match.p2.name, match.score.sets)
```

Async is the same API, awaited:

```python
from livetennisapi import AsyncLiveTennisAPI

async with AsyncLiveTennisAPI() as client:
    match = await client.get_match(18953)
```

## Command line

The package ships a `livetennis` command:

```console
$ livetennis live
Live matches (3)
ID     Tournament            Rd   Players                  Score
18953  ATP Wimbledon         R16  *Alcaraz / Sinner        6-4 3-6 2-1 (40-30)

$ livetennis match 18953
$ livetennis players djokovic
$ livetennis watch --match 18953     # live WebSocket stream
```

## Live score feed (ULTRA)

```python
from livetennisapi import LiveScoreStream

with LiveScoreStream() as stream:
    for update in stream:
        print(update.match_id, update.score.sets)
```

Reconnects automatically with backoff and re-subscribes. Heartbeats are consumed
internally, so you only see real score changes. It deliberately does **not**
reconnect on a bad key or an insufficient tier — those raise immediately rather
than retry forever.

### Break-point signals

Opt in with `signals=["break_point"]` to also receive the headline break-point
feed. The stream then yields a `BreakPoint` the moment a break point arises and a
`BreakPointResult` when it resolves, alongside the usual `ScoreUpdate`:

```python
from livetennisapi import LiveScoreStream, ScoreUpdate, BreakPoint, BreakPointResult

with LiveScoreStream(signals=["break_point"]) as stream:
    for frame in stream:
        if isinstance(frame, BreakPoint):
            print(f"BREAK POINT on match {frame.match_id}: "
                  f"p{frame.returner} has {frame.break_points} vs server p{frame.server}")
        elif isinstance(frame, BreakPointResult):
            print(f"  -> {frame.outcome} (p1 win prob now {frame.win_probability_p1_after})")
        elif isinstance(frame, ScoreUpdate):
            print(frame.match_id, frame.score.sets)
```

With no `signals` the stream behaves exactly as before — score frames only.
Both the feed and the model fields are ULTRA-only. A runnable example lives in
[`livetennisapi-starter-python`](https://github.com/livetennisapi/livetennisapi-starter-python).

## Tiers

| | FREE | BASIC | PRO | ULTRA |
|---|:--:|:--:|:--:|:--:|
| `list_matches` `get_match` `get_match_score` | ✅ | ✅ | ✅ | ✅ |
| `search_players` `get_player` `list_fixtures` | ✅ | ✅ | ✅ | ✅ |
| `list_completed_matches` (history) | — | ✅ | ✅ | ✅ |
| `list_match_events` `list_markets` `get_market_prices` | — | — | ✅ | ✅ |
| `get_match_analysis`, `win_probability_p1` / `danger`, WebSocket | — | — | — | ✅ |

Calling above your tier raises `UpgradeRequired`, which tells you which tier you need:

```python
from livetennisapi import UpgradeRequired

try:
    client.get_match_analysis(18953)
except UpgradeRequired as exc:
    print(exc.required_tier)   # 'ULTRA'
```

## Errors

| Exception | When |
|---|---|
| `Unauthorized` | 401 — key missing, unknown, or disabled |
| `UpgradeRequired` | 403 — valid key, tier too low (carries `.required_tier`) |
| `NotFound` | 404 — no such resource, or no data yet |
| `RateLimited` | 429 — carries `.retry_after` in seconds |
| `ServerError` / `ServiceUnavailable` | 5xx |
| `APIConnectionError` / `APITimeoutError` | never reached the API |

All inherit from `LiveTennisAPIError`.

Requests retry automatically on **429 and 5xx only**, honouring `Retry-After`
with exponential backoff and jitter. Other 4xx are never retried — a bad key or
an unentitled tier cannot start working, and retrying only burns rate limit.

## Pagination

`limit` defaults to 50; the API rejects anything above 200. To walk everything —
`paginate()` clamps the page size for you:

```python
for player in client.paginate("search_players", search="nadal"):
    print(player.name)
```

## Forward compatibility

The API ships **additive changes within `v1`**, so this client never rejects a
field it doesn't recognise. Unknown fields stay reachable:

```python
match = client.get_match(18953)
match.raw["some_new_field"]   # present if the server sent it
match.some_new_field          # also works
```

That means a new server-side field is usable **without upgrading this package**.

## The score shape (read this one)

`games` is **player-major**, not set-major:

```python
score.games      # [[6, 3, 2], [4, 6, 1]]  ->  6-4, 3-6, 2-1
                 #  ^p1 per set  ^p2 per set
score.sets       # [1, 1]  ->  one set each
score.server     # 1 or 2
```

Indexing it the other way is the most common mistake made against this API, so
there's a helper:

```python
score.games_for_set(0)   # (6, 4)
```

## Configuration

```python
LiveTennisAPI(
    api_key="twjp_…",          # or $LIVETENNISAPI_KEY
    base_url=None,             # or $LIVETENNISAPI_BASE_URL
    timeout=30.0,
    max_retries=2,
    auth_header="bearer",      # or "x-api-key"
)
```

## Contributing

Issues and pull requests welcome at
[livetennisapi/livetennisapi-python](https://github.com/livetennisapi/livetennisapi-python).

```bash
pip install -e ".[dev]"
pytest -m "not contract"                  # unit tests, offline
LIVETENNISAPI_KEY=twjp_… pytest -m contract   # verify against the live API
```

The contract tests assert that the live API's real responses match these models.
If the API and the [spec](https://github.com/livetennisapi/openapi) disagree,
that's a bug worth reporting.

## Related

Everything in the Live Tennis API developer surface:

| | Install | Source | Package |
|---|---|---|---|
| Python client **(this repo)** | `pip install livetennisapi` | — | [package](https://pypi.org/project/livetennisapi/) |
| JavaScript / TypeScript client | `npm install livetennisapi` | [repo](https://github.com/livetennisapi/livetennisapi-js) | [package](https://www.npmjs.com/package/livetennisapi) |
| MCP server for LLM agents | `npx livetennisapi-mcp` | [repo](https://github.com/livetennisapi/livetennisapi-mcp) | [package](https://www.npmjs.com/package/livetennisapi-mcp) |
| Vercel AI SDK tools | `npm install livetennisapi-ai` | [repo](https://github.com/livetennisapi/livetennisapi-ai) | — |
| Break-point starter — Python | — | [repo](https://github.com/livetennisapi/livetennisapi-starter-python) | — |
| Break-point starter — Node | — | [repo](https://github.com/livetennisapi/livetennisapi-starter-node) | — |
| Break-point starter — Go | — | [repo](https://github.com/livetennisapi/livetennisapi-starter-go) | — |

- **API reference** — <https://docs.livetennisapi.com> ([plain-HTML version](https://docs.livetennisapi.com/reference.html), no JavaScript required)
- **OpenAPI 3.1 specification** — [livetennisapi/openapi](https://github.com/livetennisapi/openapi)
- **Products** — <https://livetennisapi.com/products>
- **Website and plans** — <https://livetennisapi.com>

## Licence

MIT — see [LICENSE](LICENSE). Use of the API service is governed by the
[Terms of Service](https://livetennisapi.com/terms).
