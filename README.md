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

The SDK ships **two** streamers, both ULTRA, both yielding the same
`ScoreUpdate` objects:

- **`LiveScoreStream`** — the native `/ws` feed. Zero setup beyond the key;
  the best quick start.
- **`PushStream`** — the high-fan-out push feed. Token-authenticated, no
  shared connection ceiling, built for scale — **recommended for continuous
  / production streaming**. See [below](#the-push-feed-pushstream).

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

### The per-point stream

Opt in with `signals=["points"]` to receive one `PointUpdate` per committed
point — who served, who won it, the score after, and the per-match `seq`
(monotonic, gapless, starting at 1). That `seq` is the same key REST serves
from `get_match_points`, so a stream and a REST read deduplicate against each
other by `seq` alone:

```python
from livetennisapi import LiveScoreStream, PointUpdate

with LiveScoreStream(signals=["points"]) as stream:
    for frame in stream:
        if isinstance(frame, PointUpdate):
            p = frame.point
            print(f"match {frame.match_id} point {p.seq}: p{p.winner} won")
```

Check `frame.pbp_coverage` (`point` | `game`) and `frame.quality` (`clean` |
`revised`) before treating the stream as one-row-per-point truth. On
`PushStream` the same frames arrive with `points=True` — see below, including
the resume machinery the push side adds.

### Model fields ride the WebSocket too

Score frames carry the same ULTRA model fields as REST — `update.score.win_probability_p1`
and `update.score.danger` are on every frame. A `None` means the model had no
output for that state, never that the feed withholds them.

### The push feed (`PushStream`)

For continuous production streaming, use the token-authenticated push feed —
it has no shared connection ceiling and is built for scale. `PushStream` does
the whole dance for you: it mints a short-lived token via `/ws-token`, connects
to the push endpoint, subscribes, answers the server's heartbeats, and on every
reconnect mints a **fresh** token before re-subscribing:

```python
from livetennisapi import PushStream

with PushStream() as stream:              # every live match (slate:all)
    for update in stream:
        print(update.match_id, update.score.sets)

with PushStream(match_ids=[18953]) as stream:   # one specific match
    ...
```

`ScoreUpdate` frames are identical to the native feed's — nested score, ULTRA
model fields and all — so switching streamers changes nothing downstream.
Score frames are complete-state and best-effort with no replay: a missed
score frame self-corrects on the next one, so there is no catch-up to run for
scores. Auth and tier refusals surface from the token mint as the SDK's
normal exceptions (`UpgradeRequired` naming ULTRA, and so on) and are never
retried — and neither are deterministic connect/subscribe refusals: an
unknown or unpermitted channel raises `PushRefused` or `Unauthorized` instead
of reconnect-looping (every doomed reconnect would mint a token against your
quota). Reads are bounded by the server's advertised heartbeat cadence, so a
silently dead connection is detected and reconnected rather than hanging the
stream forever.

**Point frames** ride the push feed too — `points=True` subscribes the point
channels (`point:slate`, or `point:match:<id>` per entry of `match_ids`) and
yields the same `PointUpdate` objects as the native feed's `points` signal.
Because point frames are events keyed by the gapless per-match `seq` (not
self-correcting state), the push side runs a resume for them —
`points_resume=True`, the default: per-match last-`seq` cursors, a REST
catch-up of everything missed on every reconnect (fetched points are yielded
**before** live frames), `seq`-dedup of the overlap, and a synchronous
gap-fill whenever a live frame lands more than one ahead of the cursor (the
optional `on_gap(match_id, expected_seq, got_seq)` callback observes gaps;
filling happens regardless). Catch-up covers matches the stream has already
seen a point for — a from-start read of a match is `iter_match_points`.

```python
with PushStream(match_ids=[18953], points=True) as stream:
    for frame in stream:
        ...
```

Honestly stated: the point channels are **server-gated**. They are subscribed
only when the token mint's own channel vocabulary advertises them; when it
does not — the server's point gate is off, or your plan lacks point streams —
`points=True` raises `PushRefused` immediately, naming that cause, instead of
guessing a channel name or reconnect-looping. And the native streamer's
opt-in `break_point` signal frames still don't exist on the push feed — if
you need those, use `LiveScoreStream`. Frame types newer than this SDK are
still yielded, as a generic `PushFrame`.

Prefer to speak the protocol yourself? `get_ws_token()` (ULTRA) hands you the
raw connection details:

```python
tok = client.get_ws_token()
tok.ws_url                       # wss://api.livetennisapi.com/connection/websocket
tok.match_channel(18953)         # "match:18953"
tok.slate_channel                # "slate:all" — every live score frame
tok.point_match_channel(18953)   # "point:match:18953", or None when not advertised
tok.point_slate_channel          # "point:slate", or None when not advertised
```

Mint a fresh token on reconnect — tokens expire with the connection. The
point helpers return `None` when the mint's vocabulary lacks the point family:
that key will not receive point frames, so there is no name worth subscribing.

## Endpoints and tiers

| | FREE | BASIC | PRO | ULTRA |
|---|:--:|:--:|:--:|:--:|
| `list_matches` `get_match` `get_match_score` | ✅² | ✅ | ✅ | ✅ |
| `search_players` `get_player` `list_fixtures` | ✅ | ✅ | ✅ | ✅ |
| `list_tournaments` `get_tournament` | ✅ | ✅ | ✅ | ✅ |
| `get_usage` (quota-exempt) | ✅ | ✅ | ✅ | ✅ |
| `list_completed_matches`, `get_match_tape` (point-by-point tape), `get_history_coverage` | — | ✅¹ | ✅ | ✅ |
| `list_archive_matches` `get_archive_match` `list_archive_players` `get_archive_career` `get_h2h` (results archive · head-to-head) | — | ✅¹ | ✅ | ✅ |
| `list_match_events` `list_markets` `get_market_prices` | — | — | ✅ | ✅ |
| `list_rankings` — full published table (no `player`) | — | — | ✅ | ✅ |
| `list_history_packages` `get_history_package` (`kind="tape"`) | — | — | ✅³ | ✅ |
| `list_rankings` — per-player as-of records (`player=`) | — | — | — | ✅ |
| history packages with a non-tape `kind` or `year=` archive listing | — | — | — | ✅³ |
| `get_match_statistics` (aces, serve split, hold/break %) | — | — | — | ✅ |
| `get_match_points` `iter_match_points` (per-point stream, REST) | — | — | — | ✅ |
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

**The point stream over REST** (`get_match_points` / `iter_match_points`,
ULTRA) is the same per-point record the streamers push, read as pages: every
committed point with `seq` greater than `after_seq`, at most 500 per page,
cursor-paged on the point sequence (`has_more` / `last_seq` — never the page
length; a live match's newest page is routinely short while more points are
coming). It works on a live match and on a completed one, and the `seq` on
each point is identical to the streamed one, so the two reads dedup against
each other. Read `covers_from_start` before treating `seq` 1 as the match's
true first point (`None` means the server didn't state it), and
`pbp_coverage` / `quality` before backtesting.

```python
for point in client.iter_match_points(18953):
    print(point.seq, point.winner, point.score)
```

**Point-in-time rankings** (`list_rankings`) answer what every other ranking
field cannot: the rank in force ON a date, per system, never collapsed across
systems. Two modes, two gates — the full published table for one `system` is
PRO; per-player as-of records (`player=`, repeatable ≤50) are ULTRA. ATP/WTA
rows carry `previous_rank` (the prior snapshot week); UTR is a rating with
null rank. `system="elo"` is served too — never included implicitly, only
when named — with its companion parameters passed through as given: `tour`
(the Elo leaderboard requires it), `surface`, `archive_player`,
`min_matches`, `activity_weeks`.

```python
page = client.list_rankings(system="atp", as_of="2026-07-01")   # PRO listing
page = client.list_rankings(player=[925, 1137])                 # ULTRA as-of
page = client.list_rankings(system="elo", tour="atp")           # Elo leaderboard
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

## Singles vs doubles, and the coverage table

Every match carries `draw`: `"singles"`, `"doubles"`, or `None` — and the
`None` is an answer, not a gap. Team ties and team exhibitions never state
which discipline a rubber was, so those matches carry a null draw rather than
a guess (`is_doubles` remains, but cannot say "unknown"). The same word
filters `list_matches`, `list_completed_matches`, `list_tournaments` and
`list_fixtures`; a null-draw row matches NEITHER filter value, so filtering
by `singles` and then by `doubles` is not everything.

```python
page = client.list_completed_matches(tour="itf", draw="singles")

cov = client.get_history_coverage()          # BASIC, or any History plan
print(cov.as_of, cov.totals)
for name, bucket in sorted((cov.buckets or {}).items()):
    print(name, bucket["point_complete"], "of", bucket["completed"])
```

`get_history_coverage()` states, per `tour_draw` bucket (`atp_singles`,
`itf_doubles`, …), how many completed matches we hold, how many carry any
tape, how many have a complete point-by-point tape available, and how many a
default read serves complete. As of 2026-08-18 the totals were: 174,393
completed matches; 171,808 (98.5%) with a tape; 91,318 (52.4%) with a
complete tape available — of which 81,196 (46.6% of completed) were served
complete on a default read. The buckets are why the draw split exists: on the
same date ITF **singles** was 51.1% point-complete while ITF **doubles** was
3.5% — a single `itf` number would have hidden both. The table is a built
artifact (`as_of` stamps the build): a 503 `coverage_unavailable` means it is
not built yet, not that coverage is zero.

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
