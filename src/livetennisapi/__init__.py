"""Official Python client for the `Live Tennis API <https://livetennisapi.com>`_.

Real-time tennis scores, players, rankings, match-winner market prices and
model win-probability for ATP, WTA, Challenger, ITF and juniors — over REST
and WebSocket.

    from livetennisapi import LiveTennisAPI

    with LiveTennisAPI() as client:          # reads LIVETENNISAPI_KEY
        for match in client.list_matches(status="live"):
            print(match.tournament, match.score.sets)

Documentation: https://docs.livetennisapi.com
"""

from __future__ import annotations

__version__ = "1.3.2"

from .client import AsyncLiveTennisAPI, LiveTennisAPI
from .errors import (
    AbuseThrottled,
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
    ArchiveCareer,
    ArchiveMatch,
    ArchiveParticipant,
    ArchivePlayerBio,
    ChartingMatch,
    ChartingPlayer,
    Event,
    Fixture,
    HeadToHead,
    HistoryPackage,
    HistoryTape,
    ListMeta,
    Market,
    Match,
    MatchStatistics,
    Model,
    Page,
    Player,
    Price,
    RallyMatch,
    RallyPoint,
    RankingRecord,
    Score,
    TapeMeta,
    TapeRow,
    Tournament,
    Usage,
    WSToken,
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
    "Tournament",
    "ArchiveMatch",
    "ArchiveParticipant",
    "ArchivePlayerBio",
    "ArchiveCareer",
    "HeadToHead",
    "TapeRow",
    "TapeMeta",
    "HistoryTape",
    "MatchStatistics",
    "RankingRecord",
    "RallyPoint",
    "RallyMatch",
    "ChartingPlayer",
    "ChartingMatch",
    "HistoryPackage",
    "WSToken",
    "Usage",
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
    "AbuseThrottled",
    "ServerError",
    "ServiceUnavailable",
    # WebSocket (lazily imported so `websockets` stays optional)
    "LiveScoreStream",
    "ScoreUpdate",
    "BreakPoint",
    "BreakPointResult",
    "StreamFrame",
]

_WS_EXPORTS = ("LiveScoreStream", "ScoreUpdate", "BreakPoint", "BreakPointResult", "StreamFrame")


def __getattr__(name: str):
    """Expose the WebSocket client lazily so ``websockets`` stays optional."""
    if name in _WS_EXPORTS:
        from . import ws

        return getattr(ws, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
