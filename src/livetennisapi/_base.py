"""Transport logic shared by the sync and async clients.

Everything that does not depend on whether I/O blocks lives here: header
construction, URL building, response decoding, error mapping, and the retry
decision. The two clients differ only in how they await.
"""

from __future__ import annotations

import os
import random
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

from .errors import AbuseThrottled, RateLimited, UpgradeRequired, error_for_status

DEFAULT_BASE_URL = "https://api.livetennisapi.com/api/public/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2

#: Endpoints that need more than the FREE floor, so a 403 can say which tier
#: is needed rather than surfacing the API's bare
#: ``{"error": "upgrade_required"}``. First matching marker wins — ``/rally``
#: sits above ``/history`` deliberately, so ``/history/matches/{id}/rally``
#: names ULTRA rather than BASIC.
_TIER_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("/analysis", "ULTRA"),
    ("/statistics", "ULTRA"),
    ("/points", "ULTRA"),
    ("/rally", "ULTRA"),
    ("/charting", "ULTRA"),
    ("/ws-token", "ULTRA"),
    ("/events", "PRO"),
    ("/markets", "PRO"),
    ("/prices", "PRO"),
    ("/history", "BASIC"),
    ("/h2h", "BASIC"),
)


def _required_tier_for(path: str, params: Mapping[str, Any] | None = None) -> str | None:
    """The lowest tier that unlocks a request, where the SDK can know it.

    A few endpoints gate on their PARAMETERS rather than their path, so the
    request's params take part in the answer:

    - ``/rankings`` — the rank-ordered listing (no ``player``) is PRO; the
      per-player as-of mode (``player=``) is ULTRA.
    - ``/history/packages`` — tape packages are PRO; any non-tape ``kind``
      (rankings, rally) and the ``year=`` archive listing are ULTRA.
    - ``/matches?status=completed`` — completed listings need BASIC even
      though ``/matches`` itself is FREE.
    """
    params = params or {}
    if "/rankings" in path:
        return "ULTRA" if params.get("player") else "PRO"
    if "/history/packages" in path:
        kind = str(params.get("kind") or "tape")
        return "ULTRA" if kind != "tape" or params.get("year") else "PRO"
    if path.rstrip("/").endswith("/matches") and "/history" not in path and params.get("status") == "completed":
        return "BASIC"
    for marker, tier in _TIER_REQUIREMENTS:
        if marker in path:
            return tier
    return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Parse ``Retry-After``. Only the delta-seconds form is emitted by the API."""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class _BaseClient:
    """Shared configuration and response handling."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        auth_header: str = "bearer",
        user_agent: str | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("LIVETENNISAPI_KEY", "")
        self.api_key = (key or "").strip()
        self.base_url = (base_url or os.environ.get("LIVETENNISAPI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))

        auth_header = (auth_header or "bearer").lower()
        if auth_header not in ("bearer", "x-api-key"):
            raise ValueError("auth_header must be 'bearer' or 'x-api-key'")
        self.auth_header = auth_header

        from . import __version__

        self.user_agent = user_agent or f"livetennisapi-python/{__version__}"

    # -- request construction -------------------------------------------------

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if self.api_key:
            if self.auth_header == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            else:
                headers["X-API-Key"] = self.api_key
        return headers

    @staticmethod
    def _params(values: Mapping[str, Any]) -> dict[str, Any]:
        """Drop ``None`` so unset arguments don't become literal 'None'."""
        return {k: v for k, v in values.items() if v is not None}

    # -- response handling ----------------------------------------------------

    @staticmethod
    def _decode(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None

    def _raise_for_status(self, response: Any, path: str, params: Mapping[str, Any] | None = None) -> None:
        """Map a non-2xx response onto the exception hierarchy."""
        status = response.status_code
        if 200 <= status < 300:
            return

        body = self._decode(response)
        headers = dict(response.headers)
        code = body.get("error") if isinstance(body, Mapping) else None
        message = str(code or getattr(response, "reason_phrase", "") or "request failed")
        url = str(getattr(response, "url", "") or self._url(path))

        cls = error_for_status(status)
        if cls is UpgradeRequired:
            raise UpgradeRequired(
                message,
                status_code=status,
                body=body,
                headers=headers,
                request_url=url,
                required_tier=_required_tier_for(path, params),
            )
        if cls is RateLimited:
            # Three distinct 429 bodies share the status code: the per-minute
            # window, the daily cap (`scope: "day"` + `resets_at`), and the
            # abuse throttle (`abuse_throttled` + `retry_at_epoch`) — the last
            # one gets its own type because "wait a minute" is exactly the
            # wrong advice for it.
            if code == "abuse_throttled":
                cls = AbuseThrottled
            raise cls(
                message,
                status_code=status,
                body=body,
                headers=headers,
                request_url=url,
                retry_after=_retry_after_seconds(headers),
            )
        raise cls(message, status_code=status, body=body, headers=headers, request_url=url)

    # -- retry policy ---------------------------------------------------------

    def _should_retry(self, status: int) -> bool:
        """Retry only what retrying can fix.

        429 and 5xx are transient. Every other 4xx is a client-side mistake —
        a bad key, an unentitled tier, a missing id — and retrying it just
        burns rate-limit budget against a request that cannot start working.
        """
        return status == 429 or status >= 500

    def _retryable(self, response: Any) -> bool:
        """Whether an in-flight retry could plausibly succeed.

        Refines :meth:`_should_retry` with the response BODY: two 429 shapes
        cannot clear within any sane backoff window and are surfaced
        immediately instead — the daily cap (``scope: "day"``, which holds
        until the ``resets_at`` instant, typically hours away) and the abuse
        throttle (``abuse_throttled``, a 24-hour block for chronic over-cap
        clients; retrying it is the very behaviour that earns it).
        """
        status = response.status_code
        if not self._should_retry(status):
            return False
        if status == 429:
            body = self._decode(response)
            if isinstance(body, Mapping) and (body.get("error") == "abuse_throttled" or body.get("scope") == "day"):
                return False
        return True

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before the next attempt.

        Honours the server's ``Retry-After`` when present, since the API knows
        its own window better than any local heuristic. Otherwise exponential
        with full jitter, so concurrent clients don't retry in lockstep.
        """
        if retry_after is not None:
            return min(retry_after, 60.0)
        return min(0.5 * (2**attempt) + random.random() * 0.25, 10.0)
