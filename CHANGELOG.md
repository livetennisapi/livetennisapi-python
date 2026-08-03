# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.0] — 2026-08-03

### Added
- **The results archive (1968–2022).** Five new methods (on both clients) over
  the licensed historical results corpus — ATP and WTA, main draws, qualifying
  and the ITF/futures tiers, ending 2022-12-31 exactly where the
  point-by-point tape (2023→now) begins:
  - `list_archive_matches()` / `get_archive_match()` — winner/loser-shaped
    results with final score, seeds, ranks at the time, and (on the detail
    read) per-match serve statistics where the era recorded them.
    `event_date` is the tournament START date.
  - `list_archive_players()` — archive bios: hand, DOB, country, height,
    career-high rank and the earliest week it was reached.
  - `get_archive_career(name)` — career aggregates: W-L by
    surface/level/year, titles, summed serve stats with honest coverage
    (`serve["matches_with_stats"]`).
  - `get_h2h(p1, p2)` — cross-era head-to-head over the archive PLUS our own
    completed matches, name-keyed; each meeting's `winner` is 1|2 of the
    request. Ambiguous name fragments raise `BadRequest` with
    `error_code == "ambiguous_name"` and the candidate list in
    `exc.body["candidates"]` (also true of `get_archive_career`); all four
    BASIC-gated reads name `BASIC` on a 403.
- **Tournament catalogue.** `list_tournaments()` / `get_tournament(id)`
  (FREE) — the stable id space `Match.tournament_id` joins, with `surface`,
  `indoor`, curated `city`/`country`, and `category` (set only where the
  catalogues agree unambiguously, never derived from the name).
- **New list filters.** `list_matches()` takes `tour`, `player` (an id or a
  list of ids — repeated on the wire, max 50), `from_` / `to` (play-date
  bounds; `from` is a Python keyword, the wire parameter is still `from`) and
  `country` (IOC-style lowercase 3-letter codes, as `player.country` returns
  them — not ISO-3166); `list_completed_matches()` takes those plus
  `coverage`.
- **New match fields, typed.** `Match` gains `tour` (the same vocabulary the
  filter accepts), `tournament_id`, `round_code` (normalized round — the
  field to branch on) and `withdrew`. `Fixture` gains `start_time`,
  `player1_id` / `player2_id` and `round_code`. `ListMeta` gains `total` and
  `has_more`.
- New models, all exported: `Tournament`, `ArchiveMatch`,
  `ArchiveParticipant`, `ArchivePlayerBio`, `ArchiveCareer`, `HeadToHead`.

### Notes
- **Fully backwards compatible.** Every addition is a new method, a new
  optional keyword argument, or a new optional field following the same
  forward-compatible rules as the rest.

## [1.1.0] — 2026-07-24

### Added
- **Break-point signals over the WebSocket feed.** `LiveScoreStream` takes a new
  `signals=` argument; pass `signals=["break_point"]` and the stream also yields
  a `BreakPoint` the instant a break point arises and a `BreakPointResult` when
  it resolves, alongside the usual `ScoreUpdate`. Previously the subscribe frame
  carried no `signals` key and `listen()` swallowed every non-`score` frame, so
  the headline break-point feed was unreachable from this client. Switch on the
  yielded object's type (or its `.type` field) to tell frames apart.
- `BreakPoint`, `BreakPointResult` and the `StreamFrame` union are exported from
  the package (lazily, so `websockets` stays optional). Both models follow the
  same forward-compatible rules as the rest — unknown fields are preserved in
  `.raw` and readable as attributes.

### Notes
- **Fully backwards compatible.** With no `signals` (the default) the subscribe
  frame and everything `listen()` yields are byte-for-byte identical to 1.0.2 —
  score frames only.
- The break-point feed is **ULTRA-only**, like the rest of the WebSocket surface.

## [1.0.2] — 2026-07-21

### Fixed
- **A 403 on `list_completed_matches()` could not be attributed to a tier.**
  `/history/matches` used to be the entitlement floor, so nothing needed to name
  a tier for it. With the new FREE tier below it, a free key calling that method
  got an `UpgradeRequired` with no `required_tier`, leaving the caller with the
  API's bare `upgrade_required` and no idea which plan to buy. `/history` now
  maps to `BASIC`.

## [1.0.1] — 2026-07-19

### Fixed
- **WebSocket backoff never grew against a flapping server.** The retry counter
  reset on a successful *subscribe*, so a server that accepted then immediately
  dropped the socket pinned the delay at step one forever and
  `max_reconnect_attempts` was never reached. The counter now resets only after
  a connection has stayed up for 60s.
- **WebSocket leaked a socket per failed handshake.** `send`/`recv` during the
  subscribe exchange were outside any `try`, so a recv timeout or an early close
  escaped with the socket still open — once per reconnect attempt, indefinitely.
- `livetennis --json` was honoured by only three of the eight subcommands, and
  `livetennis live --json` was an argparse error. Every command now emits JSON,
  and `--json` works before or after the subcommand.
- `format_score` used `zip`, silently dropping the in-progress set when the two
  per-player game lists differed in length. Now uses `zip_longest`, matching the
  JavaScript client.
- `livetennisapi.AsyncLiveScoreStream` was advertised by the lazy importer but
  never existed, producing a confusing `ImportError`. `LiveScoreStream` and
  `ScoreUpdate` are now correctly exported instead.
- `[tool.mypy] python_version = "3.9"` made mypy ≥1.18 refuse to run at all.
  Removed; ruff's `target-version` already enforces 3.9-compatible syntax.

## [1.0.0] — 2026-07-19
## [1.0.0] — 2026-07-19

First release.

### Added
- `LiveTennisAPI` and `AsyncLiveTennisAPI` covering all 12 REST endpoints.
- `LiveScoreStream` — reconnecting WebSocket live-score feed (ULTRA).
- `livetennis` CLI: `health`, `live`, `match`, `score`, `players`, `fixtures`,
  `history`, `watch`.
- Typed error hierarchy. `UpgradeRequired` carries `.required_tier`;
  `RateLimited` carries `.retry_after`.
- Automatic retries on 429 and 5xx only, honouring `Retry-After` with
  exponential backoff and jitter. Other 4xx are never retried.
- `paginate()` for walking list endpoints on both clients.
- Full type hints and a `py.typed` marker.

### Notes
- **Models never reject unknown fields.** The API ships additive changes within
  `v1`, so unrecognised fields are preserved in `.raw` and readable as
  attributes — a new server field works without upgrading this package.
- `Score.games` is **player-major** (`[games_p1, games_p2]`, each a per-set
  list). `Score.games_for_set()` reads it safely.

First release.

### Added
- `LiveTennisAPI` and `AsyncLiveTennisAPI` covering all 12 REST endpoints.
- `LiveScoreStream` — reconnecting WebSocket live-score feed (ULTRA).
- `livetennis` CLI: `health`, `live`, `match`, `score`, `players`, `fixtures`,
  `history`, `watch`.
- Typed error hierarchy. `UpgradeRequired` carries `.required_tier`;
  `RateLimited` carries `.retry_after`.
- Automatic retries on 429 and 5xx only, honouring `Retry-After` with
  exponential backoff and jitter. Other 4xx are never retried.
- `paginate()` for walking list endpoints on both clients.
- Full type hints and a `py.typed` marker.

### Notes
- **Models never reject unknown fields.** The API ships additive changes within
  `v1`, so unrecognised fields are preserved in `.raw` and readable as
  attributes — a new server field works without upgrading this package.
- `Score.games` is **player-major** (`[games_p1, games_p2]`, each a per-set
  list). `Score.games_for_set()` reads it safely.
