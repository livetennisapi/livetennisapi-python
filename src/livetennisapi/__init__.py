"""Official Python client for the `Live Tennis API <https://livetennisapi.com>`_.

Real-time tennis scores, players, rankings, match-winner market prices and
model win-probability for ATP, WTA, Challenger and ITF — over REST and
WebSocket.

    from livetennisapi import LiveTennisAPI

    with LiveTennisAPI() as client:          # reads LIVETENNISAPI_KEY
        for match in client.list_matches(status="live"):
            print(match.tournament, match.score.sets)

Documentation: https://docs.livetennisapi.com
"""

from __future__ import annotations

__version__ = "1.0.2"

from .client import AsyncLiveTennisAPI, LiveTennisAPI
from .errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequest,
    LiveTennisAPIError,
    NotFound,
    RateLimited,
    ServerError,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from .models import (
    Analysis,
    Event,
    Fixture,
    ListMeta,
    Market,
    Match,
    Model,
    Page,
    Player,
    Price,
    Score,
)

__all__ = [
    "__version__",
    # clients
    "LiveTennisAPI",
    "AsyncLiveTennisAPI",
    # models
    "Model",
    "Page",
    "ListMeta",
    "Match",
    "Player",
    "Score",
    "Market",
    "Price",
    "Event",
    "Fixture",
    "Analysis",
    # errors
    "LiveTennisAPIError",
    "APIStatusError",
    "APIConnectionError",
    "APITimeoutError",
    "BadRequest",
    "Unauthorized",
    "UpgradeRequired",
    "NotFound",
    "RateLimited",
    "ServerError",
    "ServiceUnavailable",
    # WebSocket (lazily imported so `websockets` stays optional)
    "LiveScoreStream",
    "ScoreUpdate",
]


def __getattr__(name: str):
    """Expose the WebSocket client lazily so ``websockets`` stays optional."""
    if name in ("LiveScoreStream", "ScoreUpdate"):
        from . import ws

        return getattr(ws, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
