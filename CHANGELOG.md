# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

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
