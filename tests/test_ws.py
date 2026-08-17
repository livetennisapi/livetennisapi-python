"""WebSocket stream: subscribe frame, signals, and frame dispatch.

These use a fake socket in place of the real ``websockets`` connection, so they
assert the client's protocol behaviour without a network: what it sends to
subscribe, and which typed object it yields for each frame it receives.
"""

from __future__ import annotations

import json
from datetime import datetime

import livetennisapi.ws as ws_module
from livetennisapi import BreakPoint, BreakPointResult, LivePoint, LiveScoreStream, PointUpdate, ScoreUpdate


class FakeWS:
    """Stands in for a ``websockets`` sync connection.

    Records what the client sends, hands back a ``subscribed`` ack on ``recv``,
    then yields the given post-subscribe frames when iterated.
    """

    def __init__(self, ack: dict, frames: list[dict]) -> None:
        self._ack = ack
        self._frames = frames
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self, timeout=None):
        return json.dumps(self._ack)

    def __iter__(self):
        return (json.dumps(f) for f in self._frames)

    def close(self) -> None:
        self.closed = True


def run_stream(monkeypatch, *, signals=(), frames=None, ack=None):
    """Drive a stream against a FakeWS and return (yielded, subscribe_frame)."""
    frames = frames if frames is not None else []
    ack = ack if ack is not None else {"type": "subscribed", "topics": ["live-scores"]}
    fake = FakeWS(ack, frames)

    import websockets.sync.client as sync_client

    monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fake)

    stream = LiveScoreStream(
        api_key="twjp_test",
        signals=signals,
        auto_reconnect=False,
    )
    yielded = list(stream)
    subscribe = json.loads(fake.sent[0]) if fake.sent else None
    return yielded, subscribe


class TestBreakPointModels:
    def test_break_point_parses_every_field(self):
        bp = BreakPoint.from_dict(
            {
                "type": "break_point",
                "match_id": 18953,
                "server": 1,
                "returner": 2,
                "break_points": 2,
                "set": 3,
                "game": 9,
                "point": "30-40",
                "win_probability_p1": 0.41,
                "prob_swing": 0.22,
                "server_side_favoured": False,
                "ts": "2026-07-24T14:30:00Z",
            }
        )
        assert bp.type == "break_point"
        assert bp.match_id == 18953
        assert bp.server == 1 and bp.returner == 2
        assert bp.break_points == 2
        assert bp.point == "30-40"
        assert bp.server_side_favoured is False
        assert isinstance(bp.ts, datetime) and bp.ts.tzinfo is not None

    def test_break_point_result_parses(self):
        bpr = BreakPointResult.from_dict(
            {
                "type": "break_point_result",
                "match_id": 18953,
                "server": 1,
                "outcome": "broken",
                "win_probability_p1_after": 0.63,
                "ts": "2026-07-24T14:31:00Z",
            }
        )
        assert bpr.outcome == "broken"
        assert bpr.win_probability_p1_after == 0.63
        assert isinstance(bpr.ts, datetime)

    def test_unknown_field_is_preserved(self):
        bp = BreakPoint.from_dict({"match_id": 1, "future_field": "x"})
        assert bp.future_field == "x"
        assert bp.raw["future_field"] == "x"

    def test_none_in_none_out(self):
        assert BreakPoint.from_dict(None) is None
        assert BreakPointResult.from_dict(None) is None


class TestSubscribeFrame:
    def test_no_signals_by_default_is_backwards_compatible(self, monkeypatch):
        _, subscribe = run_stream(monkeypatch)
        assert subscribe == {"action": "subscribe", "topics": ["live-scores"]}
        assert "signals" not in subscribe

    def test_signals_are_sent_when_requested(self, monkeypatch):
        _, subscribe = run_stream(monkeypatch, signals=["break_point"])
        assert subscribe["topics"] == ["live-scores"]
        assert subscribe["signals"] == ["break_point"]

    def test_empty_strings_are_dropped_from_signals(self, monkeypatch):
        _, subscribe = run_stream(monkeypatch, signals=["", "break_point", ""])
        assert subscribe["signals"] == ["break_point"]


class TestScoreFrameShape:
    """The wire NESTS the score object; 1.3.0 parsed the whole frame instead
    and every ``update.score`` field came back None on real frames."""

    #: A frame exactly as the server's build_score_frame emits it:
    #: ``{"type", "match_id", "score": {…}}`` with the ULTRA model fields
    #: INSIDE the score object.
    REAL_FRAME = {
        "type": "score",
        "match_id": 18953,
        "score": {
            "sets": [1, 0],
            "games": [[6, 3], [4, 2]],
            "points": ["40", "30"],
            "server": 1,
            "is_tiebreak": False,
            "win_probability_p1": 0.71,
            "danger": 0.12,
            "timestamp": "2026-08-07T14:30:00Z",
        },
    }

    def test_nested_score_frame_parses(self, monkeypatch):
        yielded, _ = run_stream(monkeypatch, frames=[self.REAL_FRAME])
        assert len(yielded) == 1
        update = yielded[0]
        assert isinstance(update, ScoreUpdate)
        assert update.match_id == 18953
        assert update.score.sets == [1, 0]
        assert update.score.games_for_set(0) == (6, 4)
        assert update.score.points == ["40", "30"]
        assert update.score.server == 1
        assert isinstance(update.score.timestamp, datetime)

    def test_model_fields_ride_inside_the_nested_score(self, monkeypatch):
        yielded, _ = run_stream(monkeypatch, frames=[self.REAL_FRAME])
        assert yielded[0].score.win_probability_p1 == 0.71
        assert yielded[0].score.danger == 0.12

    def test_nested_score_update_from_dict_directly(self):
        update = ScoreUpdate.from_dict(self.REAL_FRAME)
        assert update.score.sets == [1, 0]
        assert update.score.win_probability_p1 == 0.71
        # The full frame stays reachable, exactly as received.
        assert update.raw == self.REAL_FRAME

    def test_flat_frame_still_parses_as_a_fallback(self):
        # Defensive tolerance for a flat emitter — never an all-None score.
        update = ScoreUpdate.from_dict({"type": "score", "match_id": 7, "sets": [0, 1]})
        assert update.match_id == 7
        assert update.score.sets == [0, 1]


class TestFrameDispatch:
    def test_score_frame_yields_score_update(self, monkeypatch):
        frames = [{"type": "score", "match_id": 1, "score": {"sets": [1, 0], "games": [[6], [4]]}}]
        yielded, _ = run_stream(monkeypatch, frames=frames)
        assert len(yielded) == 1
        assert isinstance(yielded[0], ScoreUpdate)
        assert yielded[0].match_id == 1
        assert yielded[0].score.sets == [1, 0]

    def test_break_frames_yield_typed_objects(self, monkeypatch):
        frames = [
            {"type": "break_point", "match_id": 1, "returner": 2, "break_points": 1},
            {"type": "break_point_result", "match_id": 1, "outcome": "held"},
        ]
        yielded, _ = run_stream(monkeypatch, signals=["break_point"], frames=frames)
        assert isinstance(yielded[0], BreakPoint)
        assert yielded[0].returner == 2
        assert isinstance(yielded[1], BreakPointResult)
        assert yielded[1].outcome == "held"

    def test_ping_and_subscribed_frames_are_swallowed(self, monkeypatch):
        frames = [
            {"type": "ping"},
            {"type": "score", "match_id": 7},
            {"type": "subscribed", "topics": ["live-scores"]},
        ]
        yielded, _ = run_stream(monkeypatch, frames=frames)
        assert len(yielded) == 1
        assert isinstance(yielded[0], ScoreUpdate)
        assert yielded[0].match_id == 7

    def test_mixed_stream_preserves_order_and_types(self, monkeypatch):
        frames = [
            {"type": "score", "match_id": 1, "score": {"sets": [0, 0]}},
            {"type": "break_point", "match_id": 1, "break_points": 1},
            {"type": "break_point_result", "match_id": 1, "outcome": "broken"},
            {"type": "score", "match_id": 1, "score": {"sets": [0, 1]}},
        ]
        yielded, _ = run_stream(monkeypatch, signals=["break_point"], frames=frames)
        assert [type(f) for f in yielded] == [ScoreUpdate, BreakPoint, BreakPointResult, ScoreUpdate]


class TestPointFrames:
    """``point`` frames were silently DROPPED before 1.5.0 — a subscription
    that asked for points must actually see them."""

    POINT_FRAME = {
        "type": "point",
        "match_id": 18953,
        "point": {
            "seq": 41,
            "set": 2,
            "game": 5,
            "number": 3,
            "tiebreak": False,
            "server": 1,
            "winner": 2,
            "score": {"p1": "30", "p2": "40"},
            "sets": [1, 0],
            "games": [[6, 2], [4, 3]],
            "ts": "2026-08-17T10:15:03Z",
        },
        "pbp_coverage": "point",
        "quality": "clean",
    }

    def test_points_signal_is_sent_when_requested(self, monkeypatch):
        _, subscribe = run_stream(monkeypatch, signals=["points"])
        assert subscribe["signals"] == ["points"]

    def test_point_frame_yields_point_update_with_nested_live_point(self, monkeypatch):
        yielded, _ = run_stream(monkeypatch, signals=["points"], frames=[self.POINT_FRAME])
        assert len(yielded) == 1
        update = yielded[0]
        assert isinstance(update, PointUpdate)
        assert update.match_id == 18953
        assert update.pbp_coverage == "point"
        assert update.quality == "clean"
        assert isinstance(update.point, LivePoint)
        assert update.point.seq == 41  # the REST-identical dedup/resume key
        assert update.point.winner == 2
        assert isinstance(update.point.ts, datetime)

    def test_point_frames_interleave_with_scores_in_order(self, monkeypatch):
        frames = [
            {"type": "score", "match_id": 18953, "score": {"sets": [1, 0]}},
            self.POINT_FRAME,
            {"type": "score", "match_id": 18953, "score": {"sets": [1, 0]}},
        ]
        yielded, _ = run_stream(monkeypatch, signals=["points"], frames=frames)
        assert [type(f) for f in yielded] == [ScoreUpdate, PointUpdate, ScoreUpdate]


def test_stream_frame_union_is_exported():
    from livetennisapi import StreamFrame  # noqa: F401


def test_ws_module_all_lists_new_symbols():
    for name in ("BreakPoint", "BreakPointResult", "PointUpdate", "StreamFrame"):
        assert name in ws_module.__all__
