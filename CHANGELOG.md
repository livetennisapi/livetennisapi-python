# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

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
