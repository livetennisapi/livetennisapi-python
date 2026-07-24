"""WebSocket stream: subscribe frame, signals, and frame dispatch.

These use a fake socket in place of the real ``websockets`` connection, so they
assert the client's protocol behaviour without a network: what it sends to
subscribe, and which typed object it yields for each frame it receives.
"""

from __future__ import annotations

import json
from datetime import datetime

import livetennisapi.ws as ws_module
from livetennisapi import BreakPoint, BreakPointResult, LiveScoreStream, ScoreUpdate


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


class TestFrameDispatch:
    def test_score_frame_yields_score_update(self, monkeypatch):
        frames = [{"type": "score", "match_id": 1, "sets": [1, 0], "games": [[6], [4]]}]
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
            {"type": "score", "match_id": 1, "sets": [0, 0]},
            {"type": "break_point", "match_id": 1, "break_points": 1},
            {"type": "break_point_result", "match_id": 1, "outcome": "broken"},
            {"type": "score", "match_id": 1, "sets": [0, 1]},
        ]
        yielded, _ = run_stream(monkeypatch, signals=["break_point"], frames=frames)
        assert [type(f) for f in yielded] == [ScoreUpdate, BreakPoint, BreakPointResult, ScoreUpdate]


def test_stream_frame_union_is_exported():
    from livetennisapi import StreamFrame  # noqa: F401


def test_ws_module_all_lists_new_symbols():
    for name in ("BreakPoint", "BreakPointResult", "StreamFrame"):
        assert name in ws_module.__all__
