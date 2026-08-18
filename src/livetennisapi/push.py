"""Push-feed live-score client. **ULTRA tier only.**

    from livetennisapi import PushStream

    with PushStream(api_key="twjp_…") as stream:
        for update in stream:
            print(update.match_id, update.score.sets)

This is the second of the SDK's two streamers. Where :class:`~livetennisapi.LiveScoreStream`
speaks the native ``/ws`` protocol directly against the API process, this one
rides the high-fan-out push endpoint: it mints a short-lived connection token
via ``GET /ws-token`` (the same call as
:meth:`~livetennisapi.LiveTennisAPI.get_ws_token`), connects to the ``ws_url``
the mint returns, and subscribes to channels. Built for continuous production
streaming — the push tier has no shared connection ceiling. Frames are the same
score objects the native feed sends, ULTRA model fields included.

Channels
--------
``slate:all``          every live match (the default)
``match:<id>``         one specific match — pass ``match_ids=[…]``
``point:slate``        every committed point — pass ``points=True``
``point:match:<id>``   one match's points — ``points=True`` with ``match_ids``
``signal:slate``       the derived signal events — pass ``signals=[…]``
``signal:match:<id>``  one match's signals — ``signals=[…]`` with ``match_ids``

The channel names come from the mint response's own vocabulary, so the server
can evolve them without an SDK release — and the point and signal channels
are subscribed ONLY when that vocabulary advertises them. Their absence from
the mint means this key will not receive those frames (the server's gate is
off, or the plan lacks them): ``points=True`` — or a non-empty ``signals`` —
then raises :class:`~livetennisapi.PushRefused` immediately — an honest
refusal, never a retry case. Frame types newer than this SDK are still
yielded, as a generic :class:`PushFrame`.

Signals (opt-in, ULTRA)
-----------------------
Pass ``signals=["break_point"]`` — the same opt-in vocabulary as the native
streamer — to also receive the headline break-point feed: a ``break_point``
frame the instant a break point arises and a ``break_point_result`` frame
when it resolves, yielded as the same :class:`~livetennisapi.BreakPoint` and
:class:`~livetennisapi.BreakPointResult` objects the native streamer yields.
``signals=["divergence"]`` adds the model-vs-market divergence events; they
have no dedicated model (on either streamer), so each arrives as a generic
:class:`PushFrame` with ``type == "divergence"``. Divergence frames are
additionally gated on a server-side flag (exactly as on the native feed): a
subscribed stream may legitimately carry none while that flag is off. The signal channels carry
every family, so the stream filters to the families you asked for — the
exact behaviour of the native ``signals`` subscription. Signal frames are
events emitted when they occur, with no replay: a stream that connects
mid-break-point does not receive the onset. With no ``signals`` the stream
behaves exactly as before.

Delivery model
--------------
Score frames are complete-state and best-effort with no replay: a missed
score frame self-corrects on the next one, so there is no client-side
catch-up to do for scores. Point frames are EVENTS, one per committed point,
carrying the per-match monotonic gapless ``seq`` — so for points the stream
does run a catch-up (``points_resume``, on by default): per-match last-seq
cursors, a REST read of everything missed on every reconnect (yielded before
live frames), seq-dedup of the overlap, and a synchronous gap-fill when a
live frame arrives more than one ahead of the cursor. Catch-up covers
matches this stream has already seen a point for; a from-start read of a
match is :meth:`~livetennisapi.LiveTennisAPI.iter_match_points`.

Reconnection
------------
The stream reconnects automatically with exponential backoff, minting a
**fresh** token on every reconnect (tokens expire with the connection and are
never reused), then re-subscribes to the same channels. It does **not**
reconnect on errors that retrying cannot fix — a bad key, a tier that lacks
the push feed, the service being disabled, an exhausted daily quota, a
missing ``websockets`` install, or a deterministic connect/subscribe refusal
(an unknown or unpermitted channel, say) all raise immediately, since
reconnecting would just hammer a closed door — and every doomed reconnect
would mint a token, a real REST call against your quota. Reads are bounded by
the server's own heartbeat cadence: a socket silent for about twice the
advertised ping interval is treated as dead and reconnected, so a half-open
TCP connection can never hang the stream forever.

Requires the ``websockets`` package::

    pip install "livetennisapi[ws]"
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union

from ._base import _BaseClient
from .errors import (
    AbuseThrottled,
    APIConnectionError,
    LiveTennisAPIError,
    MissingDependencyError,
    NotFound,
    PushRefused,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from .models import Model, WSToken
from .ws import BreakPoint, BreakPointResult, PointUpdate, ScoreUpdate

try:  # `websockets` is an optional extra; _connect() raises helpfully without it
    from websockets.exceptions import ConnectionClosedOK as _ConnectionClosedOK
except ImportError:  # pragma: no cover — only hit on a bare install

    class _ConnectionClosedOK(Exception):  # type: ignore[no-redef]
        """Stand-in when ``websockets`` is absent; never raised in that case."""


__all__ = ["PushFrame", "PushStream", "PushStreamFrame"]

#: How long the connect/subscribe handshake may take before the attempt is
#: abandoned. Mirrors the native streamer's subscribe timeout.
_HANDSHAKE_TIMEOUT_S = 15.0

#: How long a connection must stay up before it counts as healthy enough to
#: reset the backoff. Same reasoning as the native streamer: resetting on a
#: successful handshake alone lets a flapping server pin the delay at step one
#: forever, so the backoff never grows and ``max_reconnect_attempts`` is never
#: reached.
_HEALTHY_UPTIME_S = 60.0

#: Steady-state read deadline when the connect reply does not advertise the
#: server's app-ping cadence. The server pings roughly every 25s, so a socket
#: with a minute of silence is a dead link (half-open TCP after a NAT-state
#: expiry, a server host gone without a FIN) — treat it as dropped and let the
#: reconnect machinery take over instead of blocking in recv forever. When the
#: connect reply carries its ``ping`` interval, twice that is used instead.
_DEFAULT_IDLE_TIMEOUT_S = 60.0

#: Longest the reconnect loop honours a server ``Retry-After`` hint before
#: falling back to its own capped backoff — same guard as the REST client's.
_RETRY_AFTER_CAP_S = 60.0

#: How long a tracked match may go without a single frame (live or fetched)
#: before its resume cursor is evicted. Cursors otherwise accumulate forever
#: on a long-lived slate stream, and every reconnect then spends one metered
#: REST call per match ever seen — matches that finished days ago included.
#: Two hours comfortably exceeds any between-points interval in a live match;
#: a match suspended LONGER than this and resumed is simply treated as newly
#: seen again (cursor starts at its next live frame — the same documented
#: behaviour as any match first discovered mid-play). The only loss window is
#: a match that was evicted, then resumed AND ended entirely inside a later
#: disconnect — narrow enough to trade for a bounded cursor set.
_CURSOR_IDLE_EVICT_S = 2 * 60 * 60.0

#: Centrifugo error-reply codes with a precise SDK exception. Mirrors the
#: native streamer's ``_FATAL`` map: refusals that reconnecting with the same
#: inputs can never fix. Any OTHER non-temporary error reply is still fatal —
#: it raises :class:`~livetennisapi.errors.PushRefused` — because a
#: deterministic refusal repeated per reconnect would mint one REST token per
#: doomed attempt, forever. Only replies the server itself marks
#: ``"temporary": true`` are retried.
_FATAL_REPLY_CODES: dict[int, type[LiveTennisAPIError]] = {
    101: Unauthorized,  # unauthorized — the connect token was refused
    103: Unauthorized,  # permission denied — the key lacks this channel
}

#: The signal families ``signals=[…]`` can name — the native streamer's own
#: opt-in vocabulary — mapped to the frame ``type`` values each one carries.
#: The signal channels carry every family, so the stream drops the KNOWN
#: kinds the caller did not ask for (never unknown kinds: a frame family
#: newer than this SDK still reaches the caller as a :class:`PushFrame`).
_SIGNAL_FAMILIES: dict[str, tuple[str, ...]] = {
    "break_point": ("break_point", "break_point_result"),
    "divergence": ("divergence",),
}


@dataclass
class PushFrame(Model):
    """A push-feed frame of a type this SDK has no dedicated model for.

    The push feed dispatches on each frame's ``type``: ``score`` frames become
    :class:`~livetennisapi.ScoreUpdate` and ``point`` frames
    :class:`~livetennisapi.PointUpdate`, exactly as on the native feed — and
    anything else becomes one of these. All fields stay reachable through
    :attr:`raw` and as attributes, per the usual forward-compatible rules, so
    a new frame family is usable without an upgrade.
    """

    type: str | None = None
    match_id: int | None = None


#: Any frame the push stream may yield. ``score`` frames arrive as
#: :class:`~livetennisapi.ScoreUpdate`; ``point`` frames (``points=True``) as
#: :class:`~livetennisapi.PointUpdate`; ``break_point`` /
#: ``break_point_result`` frames (``signals=["break_point"]``) as
#: :class:`~livetennisapi.BreakPoint` / :class:`~livetennisapi.BreakPointResult`;
#: any other frame type — ``divergence`` included — as a generic
#: :class:`PushFrame`. Switch on the concrete type or the ``.type`` field to
#: tell them apart.
PushStreamFrame = Union[ScoreUpdate, PointUpdate, BreakPoint, BreakPointResult, PushFrame]


class PushStream(_BaseClient):
    """A reconnecting subscription to the push feed."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        match_ids: Sequence[int] = (),
        channels: Sequence[str] = (),
        points: bool = False,
        points_resume: bool = True,
        signals: Sequence[str] = (),
        on_gap: Callable[[int, int, int], None] | None = None,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 0,
        **kwargs: Any,
    ) -> None:
        # `transport` is the same test hook LiveTennisAPI takes; it reaches
        # the internal client that mints tokens.
        self._transport = kwargs.pop("transport", None)
        super().__init__(api_key, **kwargs)
        #: Specific matches to follow. Empty (the default) means the slate —
        #: every live match.
        self.match_ids = [int(m) for m in match_ids]
        #: Raw extra channel names to subscribe, verbatim — an escape hatch
        #: for channel families newer than this SDK.
        self.channels = [c for c in channels if c]
        #: Also subscribe the point channels — ``point:match:{id}`` for each
        #: of ``match_ids``, or ``point:slate`` for the whole slate — ON TOP
        #: of the score channels. Subscribed only from the mint's advertised
        #: vocabulary: when the mint does not advertise the family, this key
        #: will not receive point frames (server gate off, or the plan lacks
        #: point streams) and connecting raises
        #: :class:`~livetennisapi.PushRefused` instead of guessing a name.
        self.points = bool(points)
        #: Only meaningful with ``points=True``: keep a per-match last-seq
        #: cursor, REST-catch-up every tracked match on each (re)connect
        #: (fetched points are yielded BEFORE live frames), drop any point
        #: with ``seq <= cursor`` (live or fetched), and fill a mid-stream
        #: gap synchronously before yielding the frame that revealed it.
        self.points_resume = bool(points_resume)
        #: Informational callback ``(match_id, expected_seq, got_seq)``,
        #: invoked when a live point frame reveals a gap. Filling happens
        #: regardless — the callback observes, it does not decide.
        self.on_gap = on_gap
        #: Opt-in signal families — the native streamer's own ``signals``
        #: vocabulary (``break_point``, ``divergence``). Non-empty subscribes
        #: the signal channels — ``signal:match:{id}`` for each of
        #: ``match_ids``, or ``signal:slate`` for the whole slate — ON TOP of
        #: the score channels, from the mint's advertised vocabulary only
        #: (absence raises :class:`~livetennisapi.PushRefused`, exactly like
        #: ``points=True``). The channels carry every family; the stream
        #: filters to the ones named here. Empty (the default) means no
        #: signal channels — identical to before.
        self.signals = [s for s in signals if s]
        if "points" in self.signals:
            # The native streamer spells the per-point opt-in
            # signals=["points"]; here the point channels are their own
            # opt-in with their own resume machinery. Refuse loudly rather
            # than subscribe a signal channel that will never carry points.
            raise ValueError(
                "on the push feed the per-point stream is the points=True "
                'opt-in — pass points=True instead of signals=["points"]'
            )
        unknown = [s for s in self.signals if s not in _SIGNAL_FAMILIES]
        if unknown:
            # A typo'd family would subscribe the signal channels and then
            # filter out every known kind — a silently empty stream. Refuse
            # loudly instead, naming the real vocabulary.
            raise ValueError(
                f"unknown signal families {unknown!r} — "
                f"the vocabulary is {sorted(_SIGNAL_FAMILIES)!r}"
            )
        #: Frame kinds to DROP: the known signal kinds whose family was not
        #: asked for. Only meaningful while ``signals`` is non-empty — with
        #: no opt-in nothing is filtered (a signal frame arriving via the
        #: raw ``channels`` escape hatch passes through, like points do).
        wanted_kinds = {k for s in self.signals for k in _SIGNAL_FAMILIES.get(s, ())}
        self._signal_kind_drop = (
            {k for kinds in _SIGNAL_FAMILIES.values() for k in kinds} - wanted_kinds if self.signals else set()
        )
        self.auto_reconnect = auto_reconnect
        #: 0 means retry forever.
        self.max_reconnect_attempts = max(0, int(max_reconnect_attempts))
        self._ws: Any = None
        self._rest: Any = None
        self._closed = False
        #: Steady-state read deadline; tightened per connection from the
        #: ``ping`` cadence the connect reply advertises.
        self._idle_timeout_s = _DEFAULT_IDLE_TIMEOUT_S
        #: Decoded objects received during the handshake that belong to the
        #: stream proper (a publication racing the subscribe ack, say). The
        #: listen loop drains these before reading the socket again.
        self._backlog: list[dict[str, Any]] = []
        #: Per-match last-seq cursors (``points_resume``). A match appears
        #: here once its first point has been seen; the cursor is the highest
        #: ``seq`` delivered to the caller, live or fetched.
        self._cursors: dict[int, int] = {}
        self._cursor_seen: dict[int, float] = {}

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._rest is not None:
            try:
                self._rest.close()
            except Exception:
                pass
            self._rest = None

    def __enter__(self) -> PushStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[PushStreamFrame]:
        return self.listen()

    # -- token mint -----------------------------------------------------------

    def _rest_client(self) -> Any:
        """The internal REST client — mints tokens and runs point catch-ups."""
        if self._rest is None:
            from .client import LiveTennisAPI

            self._rest = LiveTennisAPI(
                self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
                auth_header=self.auth_header,
                user_agent=self.user_agent,
                transport=self._transport,
            )
        return self._rest

    def _mint(self) -> WSToken:
        """Mint a fresh connection token via ``GET /ws-token``.

        Called before EVERY connection attempt — tokens expire with the
        connection and are never reused across reconnects. Auth/tier refusals
        surface as the SDK's normal REST exceptions: an ULTRA gate raises
        :class:`~livetennisapi.UpgradeRequired` naming the tier, an unknown
        key :class:`~livetennisapi.Unauthorized`, a feed that is not
        configured :class:`~livetennisapi.ServiceUnavailable`.
        """
        token = self._rest_client().get_ws_token()
        if token is None or not token.token or not token.ws_url:
            # A 200 that carries no usable token is a server-side fault a
            # reconnect loop cannot fix — typed so listen() raises it rather
            # than re-minting against the same broken response forever.
            raise ServiceUnavailable(
                "the push feed mint returned no usable token — cannot connect",
                status_code=0,
                body=None,
                request_url=None,
            )
        return token

    def _channel_names(self, token: WSToken) -> list[str]:
        """The channels this stream subscribes, in the server's own vocabulary."""
        names = [token.match_channel(mid) or f"match:{mid}" for mid in self.match_ids]
        names.extend(self.channels)
        if not names:
            names.append(token.slate_channel or "slate:all")
        if self.points:
            names.extend(self._point_channel_names(token))
        if self.signals:
            names.extend(self._signal_channel_names(token))
        return names

    def _point_channel_names(self, token: WSToken) -> list[str]:
        """Point channels, read STRICTLY from the mint's advertised vocabulary.

        The mint's ``channels`` object is the server saying which families
        this key can receive. When the point family is absent, subscribing a
        guessed name would only be refused — so ``points=True`` raises the
        typed fatal refusal here instead, naming the cause. Fatal on purpose:
        the same key gets the same vocabulary on every reconnect, and each
        doomed retry would mint a REST token against a refusal that cannot
        clear.
        """
        if self.match_ids:
            names = [token.point_match_channel(mid) for mid in self.match_ids]
            if all(name is not None for name in names):
                return [name for name in names if name is not None]
        elif token.point_slate_channel is not None:
            return [token.point_slate_channel]
        raise PushRefused(
            "points=True, but the token mint's channel vocabulary does not "
            "advertise the point channels — the server's point feature gate "
            "is off, or this key's plan does not include point streams. "
            "This key will not receive point frames until that changes."
        )

    def _signal_channel_names(self, token: WSToken) -> list[str]:
        """Signal channels, read STRICTLY from the mint's advertised vocabulary.

        Same rule as :meth:`_point_channel_names`: when the mint does not
        advertise the family, subscribing a guessed name would only be
        refused — so a non-empty ``signals`` raises the typed fatal refusal
        here instead, naming the cause. Fatal on purpose: the same key gets
        the same vocabulary on every reconnect, and each doomed retry would
        mint a REST token against a refusal that cannot clear.
        """
        if self.match_ids:
            names = [token.signal_match_channel(mid) for mid in self.match_ids]
            if all(name is not None for name in names):
                return [name for name in names if name is not None]
        elif token.signal_slate_channel is not None:
            return [token.signal_slate_channel]
        raise PushRefused(
            "signals were requested, but the token mint's channel vocabulary "
            "does not advertise the signal channels — the server's signal "
            "feature gate is off, or this key's plan does not include them. "
            "This key will not receive signal frames until that changes."
        )

    # -- protocol -------------------------------------------------------------

    def _connect(self) -> Any:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            # Typed so listen() raises instead of retrying: the package will
            # not appear between attempts. Checked before the mint on purpose —
            # a doomed connection must not spend a REST call.
            raise MissingDependencyError(
                "the push feed needs the 'websockets' package — install it with: pip install \"livetennisapi[ws]\""
            ) from exc

        token = self._mint()
        # Resolve the channel list BEFORE opening a socket: with ``points=True``
        # (or a non-empty ``signals``) and no advertised vocabulary for that
        # family this raises the fatal :class:`PushRefused` without spending a
        # doomed connect+handshake.
        channel_names = self._channel_names(token)

        try:
            ws = connect(
                token.ws_url or "",  # _mint guarantees non-empty; the `or` narrows for the type checker
                additional_headers={"User-Agent": self.user_agent},
                open_timeout=self.timeout,
                close_timeout=5,
            )
        except Exception as exc:
            raise APIConnectionError(f"could not open the push feed: {exc}") from exc

        # Everything from here must close the socket on the way out: recv can
        # raise TimeoutError or ConnectionClosed, and an escaping exception
        # would leak one socket per reconnect attempt, forever.
        self._backlog = []
        try:
            deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_S
            pending: list[dict[str, Any]] = []

            # 1) Authenticate the connection with the freshly-minted token.
            # The reply advertises the server's app-ping cadence; it bounds
            # every steady-state read so a half-open socket reads as dead
            # rather than blocking forever (see _DEFAULT_IDLE_TIMEOUT_S).
            ws.send(json.dumps({"connect": {"token": token.token}, "id": 1}))
            reply = self._await_reply(ws, 1, deadline, pending, context="connect")
            info = reply.get("connect")
            ping = info.get("ping") if isinstance(info, Mapping) else None
            if isinstance(ping, (int, float)) and not isinstance(ping, bool) and ping > 0:
                self._idle_timeout_s = 2.0 * float(ping)
            else:
                self._idle_timeout_s = _DEFAULT_IDLE_TIMEOUT_S

            # 2) Subscribe every channel. An error reply names the channel.
            for offset, channel in enumerate(channel_names):
                reply_id = 2 + offset
                ws.send(json.dumps({"subscribe": {"channel": channel}, "id": reply_id}))
                self._await_reply(ws, reply_id, deadline, pending, context=f"subscribe to {channel!r}")

            # Anything that arrived interleaved with the acks belongs to the
            # stream proper — hand it to the listen loop rather than drop it.
            self._backlog = pending
            return ws
        except BaseException:
            try:
                ws.close()
            except Exception:
                pass
            raise

    def _await_reply(
        self,
        ws: Any,
        reply_id: int,
        deadline: float,
        pending: list[dict[str, Any]],
        *,
        context: str,
    ) -> dict[str, Any]:
        """Read objects until the reply for ``reply_id`` arrives.

        Server pings (the empty object) are answered inline — the server
        disconnects a client that leaves one unanswered, handshake or not.
        Any other non-matching object is kept, in order, for the listen loop.
        Both hold for the WHOLE of a newline-batched message: the batch is
        always drained before the matched reply is acted on, so a publication
        or a ping packed after the ack is never dropped.
        """
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise APIConnectionError(f"timed out waiting for the push feed {context} reply")
            matched: dict[str, Any] | None = None
            for obj in self._decode_objects(ws.recv(timeout=remaining)):
                if not obj:  # server ping — answer promptly or be dropped
                    ws.send("{}")
                    continue
                if matched is None and obj.get("id") == reply_id:
                    matched = obj
                    continue
                pending.append(obj)
            if matched is None:
                continue
            error = matched.get("error")
            if isinstance(error, Mapping):
                raise self._reply_error(error, context)
            return matched

    @staticmethod
    def _reply_error(error: Mapping[str, Any], context: str) -> LiveTennisAPIError:
        """Build the exception for an error reply, classified for retryability.

        The server marks retryable failures ``"temporary": true``; every other
        error reply is deterministic — the same handshake against the same
        server state fails the same way — so it becomes a typed fatal
        exception the reconnect loop re-raises instead of looping on (each
        loop would mint a fresh token, a metered REST call, against a refusal
        that can never clear). See ``_FATAL_REPLY_CODES``.
        """
        code = error.get("code")
        message = f"the push feed refused {context}: [{code}] {error.get('message') or 'error'}"
        if error.get("temporary") is True:
            return LiveTennisAPIError(message)  # transient — the reconnect loop may retry
        cls = _FATAL_REPLY_CODES.get(code) if isinstance(code, int) else None
        if cls is Unauthorized:
            return Unauthorized(message, status_code=0, body=dict(error), request_url=None)
        return PushRefused(message, code=code if isinstance(code, int) else None)

    @staticmethod
    def _decode_objects(message: Any) -> list[dict[str, Any]]:
        """Decode one WebSocket message into JSON objects.

        The server MAY batch several newline-delimited JSON objects into a
        single message, so every message is split on newlines before parsing.
        Non-object lines are dropped rather than crashed on.
        """
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        if not isinstance(message, str):
            return []
        objects: list[dict[str, Any]] = []
        for line in message.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(obj, dict):
                objects.append(obj)
        return objects

    def _dispatch(self, obj: dict[str, Any]) -> Iterator[PushStreamFrame]:
        """Yield the typed frame(s) one decoded object carries, if any."""
        push = obj.get("push")
        if not isinstance(push, Mapping):
            return  # a stray reply or a shape newer than this SDK — skip it
        pub = push.get("pub")
        if not isinstance(pub, Mapping):
            return  # join/leave/etc. — not a publication
        data = pub.get("data")
        if not isinstance(data, Mapping):
            return
        # Dispatch on the frame's own type, never on the channel: the same
        # channel may carry new frame families later.
        kind = data.get("type")
        if kind in self._signal_kind_drop:
            # The signal channels carry every family; a KNOWN kind whose
            # family was not asked for is dropped — the native streamer's
            # exact behaviour, where the server only sends what was asked.
            return
        if kind == "score":
            update = ScoreUpdate.from_dict(data)
            if update is not None:
                yield update
        elif kind == "point":
            yield from self._handle_point(data)
        elif kind == "break_point":
            bp = BreakPoint.from_dict(data)
            if bp is not None:
                yield bp
        elif kind == "break_point_result":
            bpr = BreakPointResult.from_dict(data)
            if bpr is not None:
                yield bpr
        else:
            frame = PushFrame.from_dict(data)
            if frame is not None:
                yield frame

    def _handle_point(self, data: Mapping[str, Any]) -> Iterator[PushStreamFrame]:
        """One live ``point`` frame: dedup, gap-fill, cursor bookkeeping.

        With ``points_resume`` off — or on a stream that never opted in with
        ``points=True`` (a point frame arriving via the raw ``channels``
        escape hatch) — the frame passes straight through: the resume
        machinery, its dedup drops and its metered REST calls all belong to
        the ``points=True`` opt-in only. With both on, ``seq`` is the whole
        story: at or below the cursor it is a duplicate (the catch-up already
        delivered it) and is dropped; exactly cursor+1 advances; further
        ahead reveals a gap, which is filled from REST synchronously BEFORE
        the trigger frame — which then only goes out if the fill did not
        already cover it.
        """
        update = PointUpdate.from_dict(data)
        if update is None:
            return
        if not (self.points and self.points_resume):
            yield update
            return
        match_id = update.match_id
        seq = update.point.seq if update.point is not None else None
        if not isinstance(match_id, int) or not isinstance(seq, int):
            yield update  # untrackable — pass it through rather than drop it
            return
        cursor = self._cursors.get(match_id)
        self._cursor_seen[match_id] = time.monotonic()
        if cursor is None:
            # First point seen for this match: the cursor starts HERE. No
            # backfill to seq 1 — catch-up covers matches already seen; a
            # from-start read is iter_match_points().
            self._cursors[match_id] = seq
            yield update
            return
        if seq > cursor + 1:
            if self.on_gap is not None:
                self.on_gap(match_id, cursor + 1, seq)
            yield from self._catch_up(match_id)
            cursor = self._cursors.get(match_id, cursor)
        if seq <= cursor:
            return  # already delivered — by the catch-up, or a repeat frame
        self._cursors[match_id] = seq
        yield update

    def _evict_idle_cursors(self) -> None:
        """Drop resume cursors for matches silent past the eviction window.

        Keeps the reconnect catch-up fan-out proportional to matches that are
        actually in play (see :data:`_CURSOR_IDLE_EVICT_S`); an evicted match
        that later resumes is treated as newly seen, exactly like any match
        first discovered mid-play.
        """
        now = time.monotonic()
        for match_id, seen in list(self._cursor_seen.items()):
            if now - seen > _CURSOR_IDLE_EVICT_S:
                self._cursors.pop(match_id, None)
                self._cursor_seen.pop(match_id, None)

    def _catch_up(self, match_id: int) -> Iterator[PointUpdate]:
        """REST-fetch everything past this match's cursor and yield it.

        Pages via ``get_match_points(after_seq=cursor)``; every fetched point
        advances the cursor as it is yielded, so live frames arriving after
        the fill dedup against it. A page that makes no forward progress ends
        the fill rather than looping. A match the server no longer knows
        (404) has its cursor evicted instead of aborting the stream — a
        NotFound repeated per reconnect would otherwise wedge the whole
        stream in a reconnect loop that no retry can ever clear.
        """
        cursor = self._cursors.get(match_id, 0)
        rest = self._rest_client()
        while True:
            try:
                page = rest.get_match_points(match_id, after_seq=cursor)
            except NotFound:
                self._cursors.pop(match_id, None)
                self._cursor_seen.pop(match_id, None)
                return
            if page is None:
                return
            advanced = False
            for point in page.points:
                seq = point.seq
                if not isinstance(seq, int) or seq <= cursor:
                    continue  # dedup holds for fetched points too
                cursor = seq
                self._cursors[match_id] = seq
                self._cursor_seen[match_id] = time.monotonic()
                advanced = True
                update = PointUpdate.from_dict(
                    {
                        "type": "point",
                        "match_id": match_id,
                        "point": point.raw,
                        "pbp_coverage": page.pbp_coverage,
                        "quality": page.quality,
                    }
                )
                if update is not None:
                    yield update
            if not page.has_more:
                return
            next_cursor = page.last_seq if isinstance(page.last_seq, int) else cursor
            if next_cursor <= cursor and not advanced:
                return  # no forward progress — never loop on a broken page
            cursor = max(cursor, next_cursor)
            self._cursors[match_id] = cursor

    def listen(self) -> Iterator[PushStreamFrame]:
        """Yield push frames until the stream is closed.

        ``score`` frames come as :class:`~livetennisapi.ScoreUpdate` — the
        exact same shape the native streamer yields, nested score and ULTRA
        model fields included. With ``points=True``, ``point`` frames come as
        :class:`~livetennisapi.PointUpdate` — and with ``points_resume`` (the
        default), every (re)connect first REST-fetches whatever each tracked
        match committed while the socket was down, yielding those points
        BEFORE any live frame, so the per-match ``seq`` order the caller sees
        never skips. With ``signals=["break_point"]``, ``break_point`` /
        ``break_point_result`` frames come as
        :class:`~livetennisapi.BreakPoint` /
        :class:`~livetennisapi.BreakPointResult` — the same objects the
        native streamer yields; ``signals=["divergence"]`` adds the
        divergence events as generic :class:`PushFrame` objects (no
        dedicated model on either streamer). Any other frame type comes as a
        generic :class:`PushFrame`. Server pings are answered internally and
        never yielded.
        """
        attempt = 0
        while not self._closed:
            connected_at: float | None = None
            last_exc: BaseException | None = None
            retry_hint: float | None = None
            ws: Any = None
            try:
                ws = self._ws = self._connect()
                connected_at = time.monotonic()

                # Points committed while the socket was down: catch each
                # tracked match up over REST FIRST — before the handshake
                # backlog and the live socket — so fetched points always
                # precede live frames and the caller's per-match seq order
                # never runs backwards. On the first connect there are no
                # cursors yet, so this is a no-op (catch-up covers matches
                # already seen; a from-start read is iter_match_points()).
                if self.points and self.points_resume:
                    self._evict_idle_cursors()
                    for match_id in list(self._cursors):
                        yield from self._catch_up(match_id)

                # Objects that raced the handshake acks come first, in order.
                for obj in self._backlog:
                    yield from self._dispatch(obj)
                self._backlog = []

                while not self._closed:
                    try:
                        # Bounded read: the server pings at half this cadence,
                        # so this much silence means a dead link (half-open
                        # TCP) — surface it so the reconnect machinery runs
                        # instead of blocking here forever.
                        message = ws.recv(timeout=self._idle_timeout_s)
                    except _ConnectionClosedOK:
                        break  # the server — or close() — ended the stream cleanly
                    except TimeoutError as exc:
                        raise APIConnectionError(
                            f"the push feed went silent for {self._idle_timeout_s:g}s — connection presumed dead"
                        ) from exc
                    for obj in self._decode_objects(message):
                        if not obj:  # server ping — answer promptly or be dropped
                            ws.send("{}")
                            continue
                        yield from self._dispatch(obj)

            except (Unauthorized, UpgradeRequired, ServiceUnavailable, PushRefused, MissingDependencyError):
                raise  # reconnecting cannot fix any of these
            except RateLimited as exc:
                # The abuse throttle and the daily cap hold for hours; a
                # reconnect loop against either is exactly the behaviour that
                # earns the throttle. Only the per-minute window is retried —
                # and its Retry-After hint, when given, bounds the sleep below.
                if isinstance(exc, AbuseThrottled) or exc.scope == "day":
                    raise
                if self._closed:
                    return
                if not self.auto_reconnect:
                    raise
                last_exc = exc
                retry_hint = exc.retry_after
            except LiveTennisAPIError as exc:
                if self._closed:
                    return
                if not self.auto_reconnect:
                    raise
                last_exc = exc
            except Exception as exc:
                if self._closed:
                    return
                if not self.auto_reconnect:
                    raise APIConnectionError(f"push feed failed: {exc}") from exc
                last_exc = exc
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                self._ws = None

            if self._closed or not self.auto_reconnect:
                return

            # Only a connection that STAYED up resets the backoff. See
            # _HEALTHY_UPTIME_S: a server that accepts then immediately drops
            # would otherwise hold the delay at step one indefinitely.
            if connected_at is not None and (time.monotonic() - connected_at) >= _HEALTHY_UPTIME_S:
                attempt = 0

            attempt += 1
            if self.max_reconnect_attempts and attempt > self.max_reconnect_attempts:
                raise APIConnectionError(
                    f"push feed did not recover after {self.max_reconnect_attempts} attempts"
                ) from last_exc
            if retry_hint is not None and retry_hint > 0:
                # The API knows its own window better than any local
                # heuristic — same rule as the REST client's backoff.
                delay = min(retry_hint, _RETRY_AFTER_CAP_S)
            else:
                delay = min(0.5 * (2 ** min(attempt, 6)) + random.random(), 30.0)
            time.sleep(delay)
