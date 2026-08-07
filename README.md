<div align="center">

<img src="https://raw.githubusercontent.com/livetennisapi/.github/main/profile/banner.jpg" alt="Live Tennis API" width="640">

# livetennisapi

**Official Python client for the [Live Tennis API](https://livetennisapi.com).**

Real-time tennis scores, players, rankings, match-winner market prices and model
win-probability — for ATP, WTA, Challenger, ITF and juniors, over REST and WebSocket.

[![CI](https://github.com/livetennisapi/livetennisapi-python/actions/workflows/ci.yml/badge.svg)](https://github.com/livetennisapi/livetennisapi-python/actions/workflows/ci.yml)
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

### Model fields ride the WebSocket too

Score frames carry the same ULTRA model fields as REST — `update.score.win_probability_p1`
and `update.score.danger` are on every frame. A `None` means the model had no
output for that state, never that the feed withholds them.

### The push feed (`get_ws_token`)

For high-fan-out consumers there is a second, token-authenticated push
endpoint. `get_ws_token()` (ULTRA) mints a short-lived token plus the
connection details:

```python
tok = client.get_ws_token()
tok.ws_url                  # wss://api.livetennisapi.com/connection/websocket
tok.match_channel(18953)    # "match:18953"
tok.slate_channel           # "slate:all" — every live score frame
```

Frames are the same allowlist score objects the polling endpoints return.
Mint a fresh token on reconnect.

## Endpoints and tiers

| | FREE | BASIC | PRO | ULTRA |
|---|:--:|:--:|:--:|:--:|
| `list_matches` `get_match` `get_match_score` | ✅² | ✅ | ✅ | ✅ |
| `search_players` `get_player` `list_fixtures` | ✅ | ✅ | ✅ | ✅ |
| `list_tournaments` `get_tournament` | ✅ | ✅ | ✅ | ✅ |
| `get_usage` (quota-exempt) | ✅ | ✅ | ✅ | ✅ |
| `list_completed_matches`, `get_match_tape` (point-by-point tape) | — | ✅¹ | ✅ | ✅ |
| `list_archive_matches` `get_archive_match` `list_archive_players` `get_archive_career` `get_h2h` (results archive · head-to-head) | — | ✅¹ | ✅ | ✅ |
| `list_match_events` `list_markets` `get_market_prices` | — | — | ✅ | ✅ |
| `list_rankings` — full published table (no `player`) | — | — | ✅ | ✅ |
| `list_history_packages` `get_history_package` (`kind="tape"`) | — | — | ✅³ | ✅ |
| `list_rankings` — per-player as-of records (`player=`) | — | — | — | ✅ |
| history packages with a non-tape `kind` or `year=` archive listing | — | — | — | ✅³ |
| `get_match_statistics` (aces, serve split, hold/break %) | — | — | — | ✅ |
| `list_rally_matches` `get_rally_match` `get_match_rally` (shot-by-shot) | — | — | — | ✅ |
| `get_charting_player` `get_charting_match` (Match Charting Project) | — | — | — | ✅ |
| `get_match_analysis`, `win_probability_p1` / `danger`, WebSocket, `get_ws_token` | — | — | — | ✅ |

¹ Also unlocked by any History plan, which works on top of a FREE key.
² `status="completed"` needs BASIC (or any History plan); `live` and `upcoming` are FREE.
³ Year-archive exports are also unlocked by History Business or a 1-year package.

## Quotas

| Tier | Per minute | Per day | Price |
|---|--:|--:|--:|
| FREE | 30 | 100/day | $0 |
| BASIC | 60 | 1,000/day | $9.99/mo |
| PRO | 300 | 10,000/day | $29.99/mo |
| ULTRA | 600 | 500,000/day | $99.99/mo |

Every response carries `X-RateLimit-Limit` / `X-RateLimit-Remaining` /
`X-RateLimit-Reset` for the minute window, and `get_usage()` reports the day
(current to the second, plus a 30-day history) without spending quota.

A FREE key's 100/day works out to a poll every ~15 minutes — don't poll faster
on FREE. For an always-on dashboard, BASIC is the tier to start at.

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
| `RateLimited` | 429 — carries `.retry_after`; on the daily cap also `.scope == "day"`, `.limit_per_day` and `.resets_at` |
| `AbuseThrottled` | 429 `abuse_throttled` — a 24h block for chronic over-cap clients; carries `.retry_at_epoch` / `.retry_at` |
| `ServerError` / `ServiceUnavailable` | 5xx |
| `APIConnectionError` / `APITimeoutError` | never reached the API |

All inherit from `LiveTennisAPIError`; `AbuseThrottled` is a `RateLimited`,
so existing `except RateLimited` handlers keep working.

Three distinct 429 bodies share the status code, and the SDK tells them apart:

- **Minute window** — wait `.retry_after` seconds and go again. Retried
  automatically.
- **Daily cap** — `.scope == "day"`; `.resets_at` is the absolute instant the
  day quota resets, parsed to an aware `datetime`. It derives from a local
  midnight, so never assume a UTC midnight. Not auto-retried: no backoff window
  reaches it.
- **Abuse throttle** — `AbuseThrottled`. The API has watched this key hammer
  through its quota repeatedly and blocked it for 24 hours (`.retry_at` says
  until when). Never auto-retried; fix the retry loop — that loop is what
  earns the block.

Requests retry automatically on **transient failures only**: 5xx and the
minute-window 429, honouring `Retry-After` with exponential backoff and
jitter. Other 4xx are never retried — a bad key or an unentitled tier cannot
start working, and retrying only burns rate limit.

## The results archive (1968–2022) and head-to-head

Two halves, one product: the **results archive** — a licensed corpus of
completed-match results, ATP and WTA, main draws, qualifying and the
ITF/futures tiers, 1968 through 2022 — and the **point-by-point tape
(2023→now)** behind `list_completed_matches`. The archive ends exactly where
the tape begins, so no match is ever served from two datasets.

```python
# Winner/loser-shaped results with ranks and seeds AT THE TIME of the match.
for m in client.list_archive_matches(tour="atp", name="borg", round="F"):
    print(m.event_date, m.tournament, m.winner.name, m.score)

# Cross-era head-to-head — archive + our own completed matches, in one call.
h2h = client.get_h2h("federer", "nadal")
print(h2h.totals, h2h.by_surface)

# Career aggregates: W-L by surface/level/year, titles, summed serve stats.
career = client.get_archive_career("borg")
```

Three things worth knowing before you lean on it:

- **`event_date` is the tournament START date** — per-match dates do not exist
  in this era's records, and none are invented.
- **Names are the keys** for `get_h2h` and `get_archive_career` (archive
  people have no roster ids). A fragment matching more than one player raises
  `BadRequest` with `error_code == "ambiguous_name"` and the candidate list in
  `exc.body["candidates"]` — disambiguate and retry.
- **`meetings[i]["winner"]` in an H2H is 1|2 of your request** (`p1`/`p2` as
  you passed them), not of the underlying match row.

## The tape, rankings, statistics — and the shot-level layer

**The point-by-point tape** (`get_match_tape`, BASIC or any History plan) is
the sequence of score states for one match — and it works on a **live** match
too, assembled from whatever has been committed so far. `sequence="clean"`
collapses to one row per distinct score state, and only clean rows carry
`point_winner`; per-set tiebreak final scores ride along in `tape.tiebreaks`.
Check `tape.meta.coverage` and `tape.meta.point_source` before backtesting —
reconstructed rows carry a null `timestamp` and null model fields, honestly.

```python
tape = client.get_match_tape(18953, sequence="clean")
for row in tape.tape:
    print(row.sets, row.games_for_set(0), row.point_winner)
```

**Point-in-time rankings** (`list_rankings`) answer what every other ranking
field cannot: the rank in force ON a date, per system, never collapsed across
systems. Two modes, two gates — the full published table for one `system` is
PRO; per-player as-of records (`player=`, repeatable ≤50) are ULTRA. ATP/WTA
rows carry `previous_rank` (the prior snapshot week); UTR is a rating with
null rank.

```python
page = client.list_rankings(system="atp", as_of="2026-07-01")   # PRO listing
page = client.list_rankings(player=[925, 1137])                 # ULTRA as-of
```

**In-play statistics** (`get_match_statistics`, ULTRA): aces, double faults,
the serve split, hold/break percentages, break points, service and return
points — in two families (derived from the point record vs measured upstream)
that are deliberately not merged. Measured fields are omitted when absent,
never zero-filled.

**Rally construction and charting** (ULTRA) are the layer below the tape: the
tape says what the score became, `list_rally_matches` / `get_rally_match` say
how each point was played, shot by shot (its own id space — the charted corpus
reaches back decades; `get_match_rally` resolves our match ids and answers a
distinguishable 404 `not_charted`). `get_charting_player` /
`get_charting_match` serve the summed Match Charting Project stat families.

**Bulk packages** (`list_history_packages` / `get_history_package`, PRO;
non-tape kinds and `year=` listings ULTRA) are pre-built monthly exports —
JSONL is one line per match with coverage meta included, CSV one row per
point.

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

Authentication: `Authorization: Bearer <key>` is preferred; `X-API-Key`
(`auth_header="x-api-key"`) works everywhere too. The WebSocket stream sends
the key as `?token=` because the browser WebSocket API cannot set handshake
headers.

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
- **Get a free API key** — <https://livetennisapi.com/subscribe/free>
- **OpenAPI 3.1 specification** — [livetennisapi/openapi](https://github.com/livetennisapi/openapi)
- **Products** — <https://livetennisapi.com/products>
- **Website and plans** — <https://livetennisapi.com>
- **Discord** — <https://discord.gg/f8WUZHgDm6>
- **GitHub org** — <https://github.com/livetennisapi>

## Affiliate program

Know developers who need tennis data? The [affiliate program](https://affiliates.livetennisapi.com/program) pays 51% recurring commission for the life of every referred subscription — 30-day cookie, and the people you refer get 10% off.

## Licence

MIT — see [LICENSE](LICENSE). Use of the API service is governed by the
[Terms of Service](https://livetennisapi.com/terms).
