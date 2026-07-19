"""WebSocket live-score feed. **ULTRA tier only.**

    from livetennisapi import LiveScoreStream

    with LiveScoreStream(api_key="twjp_…") as stream:
        for update in stream:
            print(update.match_id, update.score.sets)

The feed pushes a ``score`` frame whenever a subscribed match's score changes,
plus a ``ping`` heartbeat roughly every 15 seconds. Heartbeats are consumed
internally and never yielded; iterate and you see score updates only.

Topics
------
``live-scores``   every live match
``match:<id>``    one specific match

Reconnection
------------
The stream reconnects automatically with exponential backoff and re-subscribes
to the same topics. It does **not** reconnect on errors that retrying cannot
fix — a bad key, a tier that lacks ``ws``, or the service being disabled all
raise immediately, since reconnecting would just hammer a closed door.

Requires the ``websockets`` package::

    pip install "livetennisapi[ws]"
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from ._base import _BaseClient
from .errors import (
    APIConnectionError,
    LiveTennisAPIError,
    ServiceUnavailable,
    Unauthorized,
    UpgradeRequired,
)
from .models import Model, Score

__all__ = ["LiveScoreStream", "ScoreUpdate"]

#: The server's subscribe timeout. Subscribing later than this drops the socket.
_SUBSCRIBE_TIMEOUT_S = 15.0

#: How long a connection must stay up before it counts as healthy enough to
#: reset the backoff. Resetting on a successful subscribe alone lets a flapping
#: server (accept -> ack -> drop) pin the delay at step one forever, so the
#: backoff never grows and ``max_reconnect_attempts`` is never reached.
_HEALTHY_UPTIME_S = 60.0

#: Error codes the server sends that reconnecting will never resolve.
_FATAL = {
    "unauthorized": Unauthorized,
    "upgrade_required": UpgradeRequired,
    "service_unavailable": ServiceUnavailable,
}


@dataclass
class ScoreUpdate(Model):
    """One ``score`` frame."""

    match_id: int | None = None
    score: Score | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ScoreUpdate | None:
        obj = super().from_dict(data)
        if obj is None:
            return None
        # The frame carries score fields inline alongside match_id, so the
        # whole frame doubles as the Score payload.
        obj.score = Score.from_dict(data)
        return obj


class LiveScoreStream(_BaseClient):
    """A reconnecting subscription to the live-score feed."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        topics: Sequence[str] = ("live-scores",),
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(api_key, **kwargs)
        self.topics = list(topics) or ["live-scores"]
        self.auto_reconnect = auto_reconnect
        #: 0 means retry forever.
        self.max_reconnect_attempts = max(0, int(max_reconnect_attempts))
        self._ws: Any = None
        self._closed = False

    # -- url ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """The ``wss://`` endpoint, with the key as a query parameter.

        The key travels in the query string rather than a header because the
        browser WebSocket API cannot set headers on the handshake; the server
        accepts either. Over TLS the query string is encrypted in transit, but
        it can still land in server logs — prefer a scoped key for streaming.
        """
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path.rstrip("/") + "/ws"
        query = urlencode({"token": self.api_key}) if self.api_key else ""
        return urlunparse((scheme, parsed.netloc, path, "", query, ""))

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def __enter__(self) -> LiveScoreStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[ScoreUpdate]:
        return self.listen()

    # -- protocol -------------------------------------------------------------

    def _connect(self) -> Any:
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover
            raise LiveTennisAPIError(
                "the WebSocket feed needs the 'websockets' package — install it with: pip install \"livetennisapi[ws]\""
            ) from exc

        try:
            ws = connect(
                self.url,
                additional_headers={"User-Agent": self.user_agent},
                open_timeout=self.timeout,
                close_timeout=5,
            )
        except Exception as exc:
            raise APIConnectionError(f"could not open the live feed: {exc}") from exc

        # Everything from here must close the socket on the way out: recv can
        # raise TimeoutError or ConnectionClosed, and an escaping exception
        # would leak one socket per reconnect attempt, forever.
        try:
            # The server drops the socket if the subscribe frame is late, so
            # send it immediately. 'action' is included for forward
            # compatibility; the server keys off 'topics'.
            ws.send(json.dumps({"action": "subscribe", "topics": self.topics}))

            deadline = time.monotonic() + _SUBSCRIBE_TIMEOUT_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise APIConnectionError("timed out waiting for the subscribe acknowledgement")
                frame = self._decode_frame(ws.recv(timeout=remaining))
                if frame is None:
                    continue
                kind = frame.get("type")
                if kind == "subscribed":
                    return ws
                if kind == "error":
                    self._raise_frame_error(frame)
        except BaseException:
            try:
                ws.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _decode_frame(message: Any) -> dict[str, Any] | None:
        if isinstance(message, (bytes, bytearray)):
            message = message.decode("utf-8", "replace")
        if not isinstance(message, str):
            return None
        try:
            frame = json.loads(message)
        except (TypeError, ValueError):
            return None
        return frame if isinstance(frame, dict) else None

    @staticmethod
    def _raise_frame_error(frame: dict[str, Any]) -> None:
        code = str(frame.get("error") or "error")
        cls = _FATAL.get(code)
        if cls is not None:
            kwargs: dict[str, Any] = {}
            if cls is UpgradeRequired:
                kwargs["required_tier"] = "ULTRA"
            raise cls(
                f"the live feed refused the connection: {code}",
                status_code=0,
                body=frame,
                request_url=None,
                **kwargs,
            )
        hint = frame.get("hint")
        raise LiveTennisAPIError(f"live feed error: {code}" + (f" — {hint}" if hint else ""))

    def listen(self) -> Iterator[ScoreUpdate]:
        """Yield score updates until the stream is closed."""
        attempt = 0
        while not self._closed:
            connected_at: float | None = None
            try:
                self._ws = self._connect()
                connected_at = time.monotonic()

                for message in self._ws:
                    if self._closed:
                        return
                    frame = self._decode_frame(message)
                    if frame is None:
                        continue
                    kind = frame.get("type")
                    if kind == "score":
                        update = ScoreUpdate.from_dict(frame)
                        if update is not None:
                            yield update
                    elif kind == "error":
                        self._raise_frame_error(frame)
                    # 'ping' and 'subscribed' are protocol noise — swallow them.

            except (Unauthorized, UpgradeRequired, ServiceUnavailable):
                raise  # reconnecting cannot fix any of these
            except LiveTennisAPIError:
                if not self.auto_reconnect or self._closed:
                    raise
            except Exception as exc:
                if not self.auto_reconnect or self._closed:
                    raise APIConnectionError(f"live feed failed: {exc}") from exc
            finally:
                if self._ws is not None:
                    try:
                        self._ws.close()
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
                raise APIConnectionError(f"live feed did not recover after {self.max_reconnect_attempts} attempts")
            time.sleep(min(0.5 * (2 ** min(attempt, 6)) + random.random(), 30.0))
