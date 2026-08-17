"""Push stream: token mint, Centrifugo handshake, pings, and frame dispatch.

These use a fake socket in place of the real ``websockets`` connection and a
mocked HTTP transport for the ``/ws-token`` mint, so they assert the client's
protocol behaviour without a network: what it sends to connect and subscribe,
that it answers server pings, that a fresh token is minted per reconnect, and
which typed object it yields for each publication it receives.
"""

from __future__ import annotations

import json
import re
import sys
import threading

import httpx
import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

import livetennisapi.push as push_module
from livetennisapi import (
    LiveTennisAPIError,
    MissingDependencyError,
    PointUpdate,
    PushFrame,
    PushRefused,
    PushStream,
    ScoreUpdate,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from livetennisapi.errors import APIConnectionError

BASE = "https://api.livetennisapi.com/api/public/v1"

#: A mint response exactly as the API returns it.
TOKEN_BODY = {
    "token": "jwt-1",
    "expires_in": 3600,
    "ws_url": "wss://api.livetennisapi.com/connection/websocket",
    "channels": {"match": "match:{match_id}", "slate": "slate:all"},
}

#: A mint whose vocabulary ALSO advertises the point channel family.
POINT_TOKEN_BODY = {
    **TOKEN_BODY,
    "channels": {
        **TOKEN_BODY["channels"],
        "point_match": "point:match:{match_id}",
        "point_slate": "point:slate",
    },
}


def pub(channel: str, data: dict) -> dict:
    """One publication push, as the server sends it."""
    return {"push": {"channel": channel, "pub": {"data": data}}}


def closed_ok() -> ConnectionClosedOK:
    """The exception the real socket raises on a clean close."""
    return ConnectionClosedOK(Close(1000, ""), None)


class FakePushWS:
    """Stands in for a ``websockets`` sync connection to the push endpoint.

    Records what the client sends and replays the given handshake replies and
    post-handshake messages on ``recv``, in order — exactly the surface the
    client uses (the client never iterates the socket: every steady-state read
    goes through ``recv(timeout=…)`` so a dead link cannot block it forever).
    Items are raw wire strings, so tests can exercise newline batching and the
    empty-object ping verbatim; an item that is an exception instance is
    raised instead, and exhaustion (or ``close()``) raises the clean-close
    exception like the real socket.
    """

    def __init__(self, replies: list, messages: list) -> None:
        self._incoming = list(replies) + list(messages)
        self.sent: list[str] = []
        self.recv_timeouts: list = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self, timeout=None) -> str:
        self.recv_timeouts.append(timeout)
        if self.closed or not self._incoming:
            raise closed_ok()
        item = self._incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class BlockingPushWS(FakePushWS):
    """A fake whose ``recv`` BLOCKS once drained — a live but quiet socket —
    until ``close()`` unblocks it, mirroring the real close-interrupt."""

    def __init__(self, replies: list, messages: list) -> None:
        super().__init__(replies, messages)
        self._unblock = threading.Event()

    def recv(self, timeout=None) -> str:
        if not self._incoming and not self.closed:
            self._unblock.wait(timeout=5)  # guard: never hang the suite
        return super().recv(timeout)

    def close(self) -> None:
        super().close()
        self._unblock.set()


def handshake_replies(subscriptions: int = 1) -> list[str]:
    """A clean connect ack followed by one subscribe ack per channel."""
    replies = [json.dumps({"id": 1, "connect": {"client": "c-1", "version": "5.4.5", "ping": 25}})]
    for offset in range(subscriptions):
        replies.append(json.dumps({"id": 2 + offset, "subscribe": {}}))
    return replies


def mint_transport(*responses: httpx.Response):
    """A transport replaying the given ``/ws-token`` responses, last one sticky."""
    queue = list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return httpx.MockTransport(handler), seen


def run_push(monkeypatch, *, messages=None, replies=None, mint=None, **kwargs):
    """Drive a stream against one FakePushWS and return (yielded, fake, mint_requests)."""
    fake = FakePushWS(replies if replies is not None else handshake_replies(), messages or [])

    import websockets.sync.client as sync_client

    monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fake)

    transport, seen = mint_transport(mint if mint is not None else httpx.Response(200, json=TOKEN_BODY))
    stream = PushStream(
        api_key="twjp_test",
        transport=transport,
        auto_reconnect=False,
        max_retries=0,
        **kwargs,
    )
    yielded = list(stream)
    return yielded, fake, seen


def run_reconnecting_push(monkeypatch, *, fake_factory, max_reconnect_attempts=1, **kwargs):
    """Drive a stream with auto_reconnect=True against a fresh fake per connect.

    Returns (connect_count_list, sleeps, mint_requests, stream); the caller
    consumes the stream itself so it can assert on the raised exception.
    """
    fakes: list[FakePushWS] = []

    def connect(*a, **k):
        fakes.append(fake_factory())
        return fakes[-1]

    import websockets.sync.client as sync_client

    monkeypatch.setattr(sync_client, "connect", connect)
    sleeps: list[float] = []
    monkeypatch.setattr(push_module.time, "sleep", lambda s: sleeps.append(s))

    mint = kwargs.pop("mint", None)
    transport, seen = mint_transport(*(mint if mint is not None else [httpx.Response(200, json=TOKEN_BODY)]))
    stream = PushStream(
        api_key="twjp_test",
        transport=transport,
        auto_reconnect=True,
        max_reconnect_attempts=max_reconnect_attempts,
        max_retries=0,
        **kwargs,
    )
    return fakes, sleeps, seen, stream


class TestHandshake:
    def test_connect_frame_carries_the_minted_token(self, monkeypatch):
        _, fake, seen = run_push(monkeypatch)
        assert json.loads(fake.sent[0]) == {"connect": {"token": "jwt-1"}, "id": 1}
        assert str(seen[0].url) == f"{BASE}/ws-token"

    def test_slate_channel_is_subscribed_by_default(self, monkeypatch):
        _, fake, _ = run_push(monkeypatch)
        assert json.loads(fake.sent[1]) == {"subscribe": {"channel": "slate:all"}, "id": 2}

    def test_match_ids_subscribe_the_server_named_channels(self, monkeypatch):
        _, fake, _ = run_push(
            monkeypatch,
            replies=handshake_replies(subscriptions=2),
            match_ids=[18953, 42],
        )
        subscribes = [json.loads(s) for s in fake.sent[1:]]
        assert subscribes == [
            {"subscribe": {"channel": "match:18953"}, "id": 2},
            {"subscribe": {"channel": "match:42"}, "id": 3},
        ]

    def test_extra_channels_are_sent_verbatim(self, monkeypatch):
        _, fake, _ = run_push(monkeypatch, channels=["point:slate"])
        assert json.loads(fake.sent[1]) == {"subscribe": {"channel": "point:slate"}, "id": 2}

    def test_subscribe_error_reply_raises(self, monkeypatch):
        replies = [
            json.dumps({"id": 1, "connect": {"client": "c-1"}}),
            json.dumps({"id": 2, "error": {"code": 103, "message": "permission denied"}}),
        ]
        with pytest.raises(LiveTennisAPIError) as exc:
            run_push(monkeypatch, replies=replies)
        assert "permission denied" in str(exc.value)
        assert "slate:all" in str(exc.value)

    def test_ping_during_handshake_is_answered(self, monkeypatch):
        replies = ["{}"] + handshake_replies()
        _, fake, _ = run_push(monkeypatch, replies=replies)
        # The ping arrived before the connect ack; the reply must still go out.
        assert "{}" in fake.sent

    def test_ack_then_publication_in_one_message_is_not_dropped(self, monkeypatch):
        # The batch TAIL after the matched ack must survive — the mirror
        # ordering of test_publication_racing_the_handshake_is_not_dropped.
        replies = [
            json.dumps({"id": 1, "connect": {"client": "c-1"}}),
            json.dumps({"id": 2, "subscribe": {}}) + "\n" + json.dumps(TestFrameDispatch.SCORE_PUB),
        ]
        yielded, _, _ = run_push(monkeypatch, replies=replies)
        assert len(yielded) == 1
        assert yielded[0].match_id == 18953

    def test_ack_then_ping_in_one_message_is_answered(self, monkeypatch):
        # A server ping batched after the ack must still get its "{}" reply,
        # or the server drops the connection for the missed pong.
        replies = [
            json.dumps({"id": 1, "connect": {"client": "c-1"}}),
            json.dumps({"id": 2, "subscribe": {}}) + "\n{}",
        ]
        _, fake, _ = run_push(monkeypatch, replies=replies)
        assert "{}" in fake.sent


class TestMintErrors:
    """Auth/tier refusals surface as the SDK's normal REST exceptions and are
    never retried by the reconnect loop — reconnecting cannot fix them."""

    def test_ultra_gate_raises_upgrade_required_naming_the_tier(self, monkeypatch):
        with pytest.raises(UpgradeRequired) as exc:
            run_push(monkeypatch, mint=httpx.Response(403, json={"error": "upgrade_required"}))
        assert exc.value.required_tier == "ULTRA"

    def test_unconfigured_feed_raises_service_unavailable(self, monkeypatch):
        with pytest.raises(ServiceUnavailable):
            run_push(monkeypatch, mint=httpx.Response(503, json={"error": "service_unavailable"}))

    def test_fatal_mint_errors_are_not_reconnected(self, monkeypatch):
        transport, seen = mint_transport(httpx.Response(403, json={"error": "upgrade_required"}))
        stream = PushStream("twjp_test", transport=transport, auto_reconnect=True, max_retries=0)
        with pytest.raises(UpgradeRequired):
            list(stream)
        assert len(seen) == 1

    def test_unusable_mint_body_raises(self, monkeypatch):
        with pytest.raises(LiveTennisAPIError) as exc:
            run_push(monkeypatch, mint=httpx.Response(200, json={"token": "jwt", "ws_url": None}))
        assert "no usable token" in str(exc.value)

    def test_missing_websockets_raises_typed_error_without_minting(self, monkeypatch):
        # A bare install (no [ws] extra) must surface the install hint
        # immediately — not silently retry an ImportError forever — and must
        # never spend a REST call on a connection that cannot happen.
        monkeypatch.setitem(sys.modules, "websockets.sync.client", None)
        transport, seen = mint_transport(httpx.Response(200, json=TOKEN_BODY))
        stream = PushStream("twjp_test", transport=transport, auto_reconnect=True, max_retries=0)
        with pytest.raises(MissingDependencyError) as exc:
            list(stream)
        assert 'pip install "livetennisapi[ws]"' in str(exc.value)
        assert seen == []

    def test_reconnect_sleep_honours_retry_after(self, monkeypatch):
        # A per-minute 429 escaping the mint carries the server's Retry-After;
        # the reconnect sleep must honour it (the API knows its window better
        # than the local backoff) instead of re-minting inside the same window.
        fakes, sleeps, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: FakePushWS(handshake_replies(), []),
            mint=[
                httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "30"}),
                httpx.Response(200, json=TOKEN_BODY),
            ],
        )
        with pytest.raises(APIConnectionError):
            list(stream)
        assert sleeps[0] == 30.0
        assert len(seen) == 2


class TestErrorReplies:
    """Deterministic connect/subscribe refusals are fatal: retrying the same
    handshake cannot succeed, and every retry would mint a REST token."""

    def sticky(self, error: dict) -> FakePushWS:
        return FakePushWS(
            [
                json.dumps({"id": 1, "connect": {"client": "c-1"}}),
                json.dumps({"id": 2, "error": error}),
            ],
            [],
        )

    def test_sticky_permission_denied_raises_immediately_with_one_mint(self, monkeypatch):
        fakes, _, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: self.sticky({"code": 103, "message": "permission denied"}),
            max_reconnect_attempts=5,
        )
        with pytest.raises(Unauthorized) as exc:
            list(stream)
        assert "[103]" in str(exc.value)
        assert "permission denied" in str(exc.value)
        assert len(seen) == 1  # one mint — the refusal was never reconnect-looped
        assert len(fakes) == 1

    def test_sticky_unknown_channel_raises_push_refused_with_one_mint(self, monkeypatch):
        fakes, _, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: self.sticky({"code": 102, "message": "unknown channel"}),
            max_reconnect_attempts=5,
            channels=["typo:slate"],
        )
        with pytest.raises(PushRefused) as exc:
            list(stream)
        assert exc.value.code == 102
        assert "typo:slate" in str(exc.value)
        assert len(seen) == 1
        assert len(fakes) == 1

    def test_temporary_error_reply_is_retried_and_chained_into_the_terminal(self, monkeypatch):
        # Only errors the server itself marks temporary are worth a retry —
        # and when attempts run out, the terminal APIConnectionError must
        # carry the actual cause instead of erasing it.
        error = {"code": 100, "message": "internal server error", "temporary": True}
        fakes, _, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: self.sticky(error),
            max_reconnect_attempts=1,
        )
        with pytest.raises(APIConnectionError) as exc:
            list(stream)
        assert "did not recover after 1 attempts" in str(exc.value)
        cause = exc.value.__cause__
        assert isinstance(cause, LiveTennisAPIError)
        assert "[100]" in str(cause)
        assert len(seen) == 2  # it DID retry the temporary refusal once


class TestFrameDispatch:
    #: A publication exactly as the push endpoint delivers it: the data is the
    #: same nested score frame the native WS sends, model fields included.
    SCORE_PUB = pub(
        "slate:all",
        {
            "type": "score",
            "match_id": 18953,
            "score": {
                "sets": [1, 0],
                "games": [[6, 3], [4, 2]],
                "points": ["40", "30"],
                "server": 1,
                "win_probability_p1": 0.71,
                "danger": 0.12,
            },
        },
    )

    def test_score_publication_yields_score_update(self, monkeypatch):
        yielded, _, _ = run_push(monkeypatch, messages=[json.dumps(self.SCORE_PUB)])
        assert len(yielded) == 1
        update = yielded[0]
        assert isinstance(update, ScoreUpdate)
        assert update.match_id == 18953
        assert update.score.sets == [1, 0]
        assert update.score.win_probability_p1 == 0.71
        assert update.score.danger == 0.12

    def test_unknown_frame_type_yields_generic_push_frame(self, monkeypatch):
        # ``point`` frames grew a dedicated model in 1.5.0, so the
        # forward-compatibility check now uses a type no SDK version knows.
        novel = pub("slate:all", {"type": "momentum", "match_id": 7, "swing": 0.4})
        yielded, _, _ = run_push(monkeypatch, messages=[json.dumps(novel)])
        assert len(yielded) == 1
        frame = yielded[0]
        assert isinstance(frame, PushFrame)
        assert frame.type == "momentum"
        assert frame.match_id == 7
        assert frame.swing == 0.4  # unknown fields stay reachable

    def test_newline_batched_message_yields_every_object(self, monkeypatch):
        second = pub("slate:all", {"type": "score", "match_id": 7, "score": {"sets": [0, 1]}})
        batched = json.dumps(self.SCORE_PUB) + "\n" + json.dumps(second)
        yielded, _, _ = run_push(monkeypatch, messages=[batched])
        assert [f.match_id for f in yielded] == [18953, 7]

    def test_server_ping_is_answered_and_never_yielded(self, monkeypatch):
        yielded, fake, _ = run_push(monkeypatch, messages=["{}", json.dumps(self.SCORE_PUB)])
        assert len(yielded) == 1
        assert fake.sent[-1] == "{}"

    def test_non_publication_pushes_are_swallowed(self, monkeypatch):
        noise = [
            json.dumps({"push": {"channel": "slate:all", "join": {"info": {}}}}),
            json.dumps({"id": 99, "subscribe": {}}),  # a stray late reply
            json.dumps(self.SCORE_PUB),
        ]
        yielded, _, _ = run_push(monkeypatch, messages=noise)
        assert len(yielded) == 1
        assert isinstance(yielded[0], ScoreUpdate)

    def test_publication_racing_the_handshake_is_not_dropped(self, monkeypatch):
        # The score arrives in the same recv stream as the subscribe ack.
        replies = [
            json.dumps({"id": 1, "connect": {"client": "c-1"}}),
            json.dumps(self.SCORE_PUB) + "\n" + json.dumps({"id": 2, "subscribe": {}}),
        ]
        yielded, _, _ = run_push(monkeypatch, replies=replies)
        assert len(yielded) == 1
        assert yielded[0].match_id == 18953


def point_pub(match_id: int, seq: int) -> dict:
    """One live ``point`` publication, exactly as the point channels carry it."""
    return pub(
        f"point:match:{match_id}",
        {
            "type": "point",
            "match_id": match_id,
            "point": {
                "seq": seq,
                "set": 1,
                "game": 1,
                "number": seq,
                "server": 1,
                "winner": 1 + (seq % 2),
                "score": {"p1": "15", "p2": "0"},
                "sets": [0, 0],
                "games": [[0], [0]],
                "ts": "2026-08-17T10:00:00Z",
            },
            "pbp_coverage": "point",
            "quality": "clean",
        },
    )


def points_body(match_id: int, seqs: list[int], *, has_more: bool = False) -> dict:
    """One REST ``/matches/{id}/points`` page body."""
    return {
        "match_id": match_id,
        "pbp_coverage": "point",
        "quality": "clean",
        "points": [{"seq": s, "winner": 1 + (s % 2), "score": {"p1": "15", "p2": "0"}} for s in seqs],
        "last_seq": seqs[-1] if seqs else None,
        "has_more": has_more,
    }


def points_transport(token_body: dict, pages_by_match: dict):
    """A transport routing ``/ws-token`` to the mint and ``/matches/{id}/points``
    to the queued page bodies (last one sticky, like ``mint_transport``)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/ws-token"):
            return httpx.Response(200, json=token_body)
        m = re.search(r"/matches/(\d+)/points$", path)
        if m:
            queue = pages_by_match.get(int(m.group(1))) or [points_body(int(m.group(1)), [])]
            return httpx.Response(200, json=queue.pop(0) if len(queue) > 1 else queue[0])
        return httpx.Response(404, json={"error": "not_found"})

    return httpx.MockTransport(handler), seen


def run_points_push(monkeypatch, *, messages=None, pages_by_match=None, token_body=None, **kwargs):
    """Drive a ``points=True`` stream (slate + point:slate — two subscribes) and
    return (yielded, fake, requests_seen)."""
    fake = FakePushWS(handshake_replies(subscriptions=2), messages or [])

    import websockets.sync.client as sync_client

    monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fake)

    transport, seen = points_transport(token_body if token_body is not None else POINT_TOKEN_BODY, pages_by_match or {})
    stream = PushStream(
        api_key="twjp_test",
        transport=transport,
        auto_reconnect=False,
        max_retries=0,
        points=True,
        **kwargs,
    )
    yielded = list(stream)
    return yielded, fake, seen


class TestPointChannels:
    """The point channels are subscribed ONLY from the mint's own vocabulary;
    their absence is an honest refusal, never a guessed name."""

    def test_points_true_subscribes_the_advertised_slate_channel(self, monkeypatch):
        _, fake, _ = run_points_push(monkeypatch)
        subscribes = [json.loads(s)["subscribe"]["channel"] for s in fake.sent[1:]]
        assert subscribes == ["slate:all", "point:slate"]

    def test_points_true_with_match_ids_subscribes_per_match_point_channels(self, monkeypatch):
        fake = FakePushWS(handshake_replies(subscriptions=2), [])

        import websockets.sync.client as sync_client

        monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fake)
        transport, _ = points_transport(POINT_TOKEN_BODY, {})
        stream = PushStream(
            "twjp_test",
            transport=transport,
            auto_reconnect=False,
            max_retries=0,
            points=True,
            match_ids=[18953],
        )
        list(stream)
        subscribes = [json.loads(s)["subscribe"]["channel"] for s in fake.sent[1:]]
        assert subscribes == ["match:18953", "point:match:18953"]

    def test_absent_vocabulary_raises_the_typed_refusal_naming_the_cause(self, monkeypatch):
        with pytest.raises(PushRefused) as exc:
            run_points_push(monkeypatch, token_body=TOKEN_BODY)
        message = str(exc.value)
        assert "point" in message
        assert "gate" in message
        assert "plan" in message

    def test_the_refusal_is_never_reconnect_looped(self, monkeypatch):
        # Same key, same vocabulary on every reconnect: retrying would only
        # mint a REST token per doomed attempt.
        fakes, _, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: FakePushWS(handshake_replies(subscriptions=2), []),
            max_reconnect_attempts=5,
            mint=[httpx.Response(200, json=TOKEN_BODY)],
            points=True,
        )
        with pytest.raises(PushRefused):
            list(stream)
        assert len(seen) == 1  # one mint — the refusal was immediate


class TestPointFrames:
    def test_point_publication_yields_point_update(self, monkeypatch):
        yielded, _, _ = run_points_push(monkeypatch, messages=[json.dumps(point_pub(7, 41))])
        assert len(yielded) == 1
        update = yielded[0]
        assert isinstance(update, PointUpdate)
        assert update.match_id == 7
        assert update.pbp_coverage == "point"
        assert update.quality == "clean"
        assert update.point.seq == 41

    def test_duplicate_seq_is_dropped(self, monkeypatch):
        messages = [json.dumps(point_pub(7, s)) for s in (5, 5, 6)]
        yielded, _, seen = run_points_push(monkeypatch, messages=messages)
        assert [f.point.seq for f in yielded] == [5, 6]
        assert not [r for r in seen if r.url.path.endswith("/points")]  # no gap → no REST

    def test_points_resume_off_passes_everything_through(self, monkeypatch):
        messages = [json.dumps(point_pub(7, s)) for s in (5, 5, 9)]
        yielded, _, seen = run_points_push(monkeypatch, messages=messages, points_resume=False)
        assert [f.point.seq for f in yielded] == [5, 5, 9]  # dups and gaps: caller's problem
        assert not [r for r in seen if r.url.path.endswith("/points")]

    def test_gap_is_filled_from_rest_before_the_trigger_frame(self, monkeypatch):
        gaps: list[tuple[int, int, int]] = []
        yielded, _, seen = run_points_push(
            monkeypatch,
            messages=[json.dumps(point_pub(7, 3)), json.dumps(point_pub(7, 7))],
            pages_by_match={7: [points_body(7, [4, 5, 6, 7])]},
            on_gap=lambda m, expected, got: gaps.append((m, expected, got)),
        )
        assert [f.point.seq for f in yielded] == [3, 4, 5, 6, 7]
        assert all(isinstance(f, PointUpdate) for f in yielded)
        assert yielded[1].pbp_coverage == "point"  # fetched frames carry the page's meta
        assert gaps == [(7, 4, 7)]  # informational only — the fill ran regardless
        point_reqs = [r for r in seen if r.url.path.endswith("/points")]
        assert len(point_reqs) == 1
        assert point_reqs[0].url.params["after_seq"] == "3"

    def test_gap_fill_follows_has_more_across_pages(self, monkeypatch):
        yielded, _, seen = run_points_push(
            monkeypatch,
            messages=[json.dumps(point_pub(7, 3)), json.dumps(point_pub(7, 7))],
            pages_by_match={
                7: [points_body(7, [4, 5], has_more=True), points_body(7, [6])],
            },
        )
        # REST held 4-6; the trigger frame itself (7) still goes out after the fill.
        assert [f.point.seq for f in yielded] == [3, 4, 5, 6, 7]
        point_reqs = [r for r in seen if r.url.path.endswith("/points")]
        assert [r.url.params["after_seq"] for r in point_reqs] == ["3", "5"]

    def test_untrackable_point_passes_through(self, monkeypatch):
        # No seq to cursor on — passing it through beats dropping it.
        frame = pub("point:slate", {"type": "point", "match_id": 7, "point": {"seq": None}})
        yielded, _, _ = run_points_push(monkeypatch, messages=[json.dumps(frame)])
        assert len(yielded) == 1
        assert isinstance(yielded[0], PointUpdate)


class TestPointResume:
    def test_reconnect_catches_up_each_tracked_match_before_live_frames(self, monkeypatch):
        # Connection 1 sees seq 2, then drops; 3 and 4 commit while the socket
        # is down; connection 2 receives seq 5 live. The caller must see
        # 2, 3, 4, 5 — the REST fill BEFORE the live frame, nothing skipped.
        fakes = [
            FakePushWS(handshake_replies(subscriptions=2), [json.dumps(point_pub(7, 2))]),
            FakePushWS(handshake_replies(subscriptions=2), [json.dumps(point_pub(7, 5))]),
        ]

        import websockets.sync.client as sync_client

        monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fakes.pop(0))
        monkeypatch.setattr(push_module.time, "sleep", lambda s: None)

        transport, seen = points_transport(POINT_TOKEN_BODY, {7: [points_body(7, [3, 4])]})
        stream = PushStream(
            "twjp_test",
            transport=transport,
            auto_reconnect=True,
            max_reconnect_attempts=1,
            max_retries=0,
            points=True,
            match_ids=[7],
        )
        frames: list = []
        with pytest.raises(APIConnectionError):
            for frame in stream:
                frames.append(frame)
        assert [f.point.seq for f in frames] == [2, 3, 4, 5]
        point_reqs = [r for r in seen if r.url.path.endswith("/points")]
        assert point_reqs[0].url.params["after_seq"] == "2"  # from the tracked cursor
        mints = [r for r in seen if r.url.path.endswith("/ws-token")]
        assert len(mints) == 2  # still one fresh token per connection

    def test_first_connect_runs_no_catch_up(self, monkeypatch):
        # No cursors yet: catch-up covers matches already seen. A from-start
        # read is iter_match_points(), not the stream.
        _, _, seen = run_points_push(monkeypatch, match_ids=[7])
        assert not [r for r in seen if r.url.path.endswith("/points")]


class TestLiveness:
    """Steady-state reads are bounded by the server's advertised ping cadence:
    a half-open socket must read as dead, not block listen() forever."""

    def test_steady_reads_are_bounded_by_twice_the_advertised_ping(self, monkeypatch):
        # handshake_replies advertises ping=25 → every post-handshake recv
        # must carry a 50s deadline.
        _, fake, _ = run_push(monkeypatch, messages=[])
        assert fake.recv_timeouts[-1] == 50.0

    def test_steady_reads_fall_back_to_the_default_deadline(self, monkeypatch):
        replies = [
            json.dumps({"id": 1, "connect": {"client": "c-1"}}),  # no ping field
            json.dumps({"id": 2, "subscribe": {}}),
        ]
        _, fake, _ = run_push(monkeypatch, replies=replies)
        assert fake.recv_timeouts[-1] == 60.0

    def test_silent_socket_is_treated_as_dead(self, monkeypatch):
        with pytest.raises(APIConnectionError) as exc:
            run_push(monkeypatch, messages=[TimeoutError("recv deadline")])
        assert "went silent" in str(exc.value)

    def test_silent_socket_reconnects_under_auto_reconnect(self, monkeypatch):
        calls = [[TimeoutError("recv deadline")], []]
        fakes, _, seen, stream = run_reconnecting_push(
            monkeypatch,
            fake_factory=lambda: FakePushWS(handshake_replies(), calls.pop(0)),
            max_reconnect_attempts=1,
        )
        with pytest.raises(APIConnectionError):
            list(stream)
        assert len(seen) == 2  # the dead link triggered a full reconnect + fresh mint
        assert len(fakes) == 2


class TestReconnect:
    def test_fresh_token_is_minted_for_every_reconnect(self, monkeypatch):
        fakes = [
            FakePushWS(handshake_replies(), [json.dumps(TestFrameDispatch.SCORE_PUB)]),
            FakePushWS(handshake_replies(), []),
        ]

        import websockets.sync.client as sync_client

        monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fakes.pop(0))
        monkeypatch.setattr(push_module.time, "sleep", lambda s: None)

        transport, seen = mint_transport(
            httpx.Response(200, json={**TOKEN_BODY, "token": "jwt-1"}),
            httpx.Response(200, json={**TOKEN_BODY, "token": "jwt-2"}),
        )
        stream = PushStream(
            "twjp_test",
            transport=transport,
            auto_reconnect=True,
            max_reconnect_attempts=1,
            max_retries=0,
        )
        fake1, fake2 = fakes[0], fakes[1]
        with pytest.raises(APIConnectionError):
            list(stream)

        assert len(seen) == 2  # one mint per connection — tokens never reused
        assert json.loads(fake1.sent[0])["connect"]["token"] == "jwt-1"
        assert json.loads(fake2.sent[0])["connect"]["token"] == "jwt-2"

    def test_close_from_another_thread_interrupts_a_blocked_listen(self, monkeypatch):
        # The contract close() actually promises: a consumer blocked in a read
        # (a quiet live socket) is unblocked, the socket is closed, and
        # iteration ends cleanly rather than raising.
        fake = BlockingPushWS(handshake_replies(), [json.dumps(TestFrameDispatch.SCORE_PUB)])

        import websockets.sync.client as sync_client

        monkeypatch.setattr(sync_client, "connect", lambda *a, **k: fake)
        transport, _ = mint_transport(httpx.Response(200, json=TOKEN_BODY))
        stream = PushStream("twjp_test", transport=transport, auto_reconnect=False, max_retries=0)

        frames: list = []
        got_one = threading.Event()

        def consume() -> None:
            for frame in stream:
                frames.append(frame)
                got_one.set()

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        assert got_one.wait(timeout=5)  # the stream is live; listen is now blocked in recv
        stream.close()
        worker.join(timeout=5)
        assert not worker.is_alive()  # close() interrupted the blocked read
        assert fake.closed
        assert len(frames) == 1
        assert isinstance(frames[0], ScoreUpdate)


def test_push_symbols_are_exported():
    from livetennisapi import PushFrame, PushStream, PushStreamFrame  # noqa: F401


def test_push_module_all_lists_public_symbols():
    for name in ("PushStream", "PushFrame", "PushStreamFrame"):
        assert name in push_module.__all__
