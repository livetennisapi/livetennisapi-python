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
``slate:all``     every live match (the default)
``match:<id>``    one specific match — pass ``match_ids=[…]``

The channel names come from the mint response's own vocabulary, so the server
can evolve them without an SDK release. Today the push feed carries **score
frames only** — the native streamer's opt-in ``break_point`` signal frames do
not exist here (yet). Unknown frame types are still yielded, as a generic
:class:`PushFrame`, so a future ``point`` channel family works without an
upgrade.

Delivery model
--------------
Frames are complete-state and best-effort with no replay: a missed frame
self-corrects on the next one, so there is no client-side catch-up to do.

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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union

from ._base import _BaseClient
from .errors import (
    AbuseThrottled,
    APIConnectionError,
    LiveTennisAPIError,
    MissingDependencyError,
    PushRefused,
    RateLimited,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from .models import Model, WSToken
from .ws import ScoreUpdate

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


@dataclass
class PushFrame(Model):
    """A push-feed frame of a type this SDK has no dedicated model for.

    The push feed dispatches on each frame's ``type``: ``score`` frames become
    :class:`~livetennisapi.ScoreUpdate` exactly as on the native feed, and
    anything else — a future ``point`` frame, say — becomes one of these. All
    fields stay reachable through :attr:`raw` and as attributes, per the usual
    forward-compatible rules, so a new frame family is usable without an
    upgrade.
    """

    type: str | None = None
    match_id: int | None = None


#: Any frame the push stream may yield. Today that is ``score`` frames as
#: :class:`~livetennisapi.ScoreUpdate`; any other frame type arrives as a
#: generic :class:`PushFrame`. Switch on the concrete type or the ``.type``
#: field to tell them apart.
PushStreamFrame = Union[ScoreUpdate, PushFrame]


class PushStream(_BaseClient):
    """A reconnecting subscription to the push feed."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        match_ids: Sequence[int] = (),
        channels: Sequence[str] = (),
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
        #: for channel families newer than this SDK (e.g. ``point:slate``).
        self.channels = [c for c in channels if c]
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

    def _mint(self) -> WSToken:
        """Mint a fresh connection token via ``GET /ws-token``.

        Called before EVERY connection attempt — tokens expire with the
        connection and are never reused across reconnects. Auth/tier refusals
        surface as the SDK's normal REST exceptions: an ULTRA gate raises
        :class:`~livetennisapi.UpgradeRequired` naming the tier, an unknown
        key :class:`~livetennisapi.Unauthorized`, a feed that is not
        configured :class:`~livetennisapi.ServiceUnavailable`.
        """
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
        token = self._rest.get_ws_token()
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
        return names

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
            for offset, channel in enumerate(self._channel_names(token)):
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
        if data.get("type") == "score":
            update = ScoreUpdate.from_dict(data)
            if update is not None:
                yield update
        else:
            frame = PushFrame.from_dict(data)
            if frame is not None:
                yield frame

    def listen(self) -> Iterator[PushStreamFrame]:
        """Yield push frames until the stream is closed.

        ``score`` frames come as :class:`~livetennisapi.ScoreUpdate` — the
        exact same shape the native streamer yields, nested score and ULTRA
        model fields included. Any other frame type comes as a generic
        :class:`PushFrame`. Server pings are answered internally and never
        yielded.
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
