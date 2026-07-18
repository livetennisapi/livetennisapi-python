"""``livetennis`` — a command-line client for the Live Tennis API.

    export LIVETENNISAPI_KEY=twjp_…
    livetennis live
    livetennis match 18953
    livetennis watch --match 18953

Uses `rich <https://github.com/Textualize/rich>`_ for tables when it is
installed and falls back to plain text when it is not, so the CLI works on a
bare ``pip install livetennisapi``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__
from .client import LiveTennisAPI
from .errors import LiveTennisAPIError, RateLimited, Unauthorized, UpgradeRequired

try:  # pragma: no cover - presentation only
    from rich.console import Console
    from rich.table import Table

    _console: Any = Console()
    _RICH = True
except ImportError:  # pragma: no cover
    _console = None
    _RICH = False


# -- rendering ----------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text)


def _render_table(title: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    if not rows:
        _out(f"{title}: nothing to show")
        return

    if _RICH:
        table = Table(title=title, title_justify="left", header_style="bold green")
        for col in columns:
            table.add_column(col, overflow="fold")
        for row in rows:
            table.add_row(*[str(c) for c in row])
        _console.print(table)
        return

    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    _out(title)
    _out("  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
    _out("  ".join("-" * widths[i] for i in range(len(columns))))
    for row in rows:
        _out("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def _format_score(score: Any) -> str:
    """Render a score as ``6-4 3-6 2-1  (40-30)``.

    ``games`` is player-major — ``[[6,3,2],[4,6,1]]`` — so sets are read by
    zipping the two lists, not by iterating one of them.
    """
    if score is None:
        return "-"
    games = getattr(score, "games", None) or []
    parts: list[str] = []
    if len(games) >= 2 and isinstance(games[0], list) and isinstance(games[1], list):
        for a, b in zip(games[0], games[1]):
            parts.append(f"{a}-{b}")
    elif getattr(score, "sets", None):
        sets = score.sets or []
        if len(sets) >= 2:
            parts.append(f"{sets[0]}-{sets[1]}")

    points = getattr(score, "points", None) or []
    if len(points) >= 2:
        parts.append(f"({points[0]}-{points[1]})")
    return " ".join(parts) or "-"


def _names(match: Any) -> tuple[str, str]:
    p1, p2 = getattr(match, "p1", None), getattr(match, "p2", None)
    return (
        (getattr(p1, "name", None) or "?"),
        (getattr(p2, "name", None) or "?"),
    )


def _server_mark(score: Any, side: int) -> str:
    return "*" if getattr(score, "server", None) == side else " "


# -- commands -----------------------------------------------------------------


def _cmd_health(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    data = client.health()
    _out(json.dumps(data, indent=2))
    return 0


def _cmd_live(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    page = client.list_matches(status=args.status, limit=args.limit)
    rows = []
    for m in page:
        n1, n2 = _names(m)
        rows.append(
            [
                m.id or "-",
                (m.tournament or "-")[:34],
                m.round or "-",
                f"{_server_mark(m.score, 1)}{n1} / {_server_mark(m.score, 2)}{n2}",
                _format_score(m.score),
            ]
        )
    _render_table(
        f"{args.status.title()} matches ({len(rows)})",
        ["ID", "Tournament", "Rd", "Players", "Score"],
        rows,
    )
    return 0


def _cmd_match(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    match = client.get_match(args.match_id)
    if match is None:
        _out("no such match")
        return 1
    if args.json:
        _out(json.dumps(match.to_dict(), indent=2, default=str))
        return 0

    n1, n2 = _names(match)
    rows = [
        ["ID", match.id or "-"],
        ["Tournament", match.tournament or "-"],
        ["Round", match.round or "-"],
        ["Surface", f"{match.surface or '-'}{' (indoor)' if match.indoor else ''}"],
        ["Format", match.format or "-"],
        ["Status", match.status or "-"],
        ["Players", f"{n1} vs {n2}"],
        ["Score", _format_score(match.score)],
    ]
    if match.winner:
        rows.append(["Winner", n1 if match.winner == 1 else n2])

    score = match.score
    if score is not None and getattr(score, "win_probability_p1", None) is not None:
        rows.append(["Win prob (p1)", f"{score.win_probability_p1:.1%}"])
    if score is not None and getattr(score, "danger", None) is not None:
        rows.append(["Danger", f"{score.danger:.3f}"])
    if match.market is not None:
        rows.append(["Market", match.market.question or "-"])

    _render_table("Match", ["Field", "Value"], rows)
    return 0


def _cmd_score(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    score = client.get_match_score(args.match_id)
    if score is None:
        _out("no score yet")
        return 1
    if args.json:
        _out(json.dumps(score.to_dict(), indent=2, default=str))
        return 0
    _out(_format_score(score))
    return 0


def _cmd_players(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    page = client.search_players(args.query, limit=args.limit)
    rows = [
        [p.id or "-", p.name or "-", p.country or "-", p.ranking if p.ranking is not None else "-", p.tour or "-"]
        for p in page
    ]
    _render_table(f"Players matching {args.query!r}", ["ID", "Name", "Country", "Rank", "Tour"], rows)
    return 0


def _cmd_fixtures(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    page = client.list_fixtures(limit=args.limit)
    rows = [
        [
            f.event_date or "-",
            (f.tournament or "-")[:30],
            f.round or "-",
            f"{f.player1_name or '?'} vs {f.player2_name or '?'}",
        ]
        for f in page
    ]
    _render_table(f"Upcoming fixtures ({len(rows)})", ["Date", "Tournament", "Rd", "Players"], rows)
    return 0


def _cmd_history(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    page = client.list_completed_matches(limit=args.limit)
    rows = []
    for m in page:
        n1, n2 = _names(m)
        won = n1 if m.winner == 1 else (n2 if m.winner == 2 else "-")
        rows.append([m.id or "-", (m.tournament or "-")[:30], f"{n1} vs {n2}", _format_score(m.score), won])
    _render_table(f"Completed matches ({len(rows)})", ["ID", "Tournament", "Players", "Score", "Winner"], rows)
    return 0


def _cmd_watch(client: LiveTennisAPI, args: argparse.Namespace) -> int:
    from .ws import LiveScoreStream

    topics = [f"match:{args.match}"] if args.match else ["live-scores"]
    _out(f"subscribing to {topics[0]} — Ctrl-C to stop")

    stream = LiveScoreStream(client.api_key, topics=topics, base_url=client.base_url)
    try:
        with stream:
            for update in stream:
                if args.json:
                    _out(json.dumps(update.to_dict(), default=str))
                else:
                    _out(f"[{update.match_id}] {_format_score(update.score)}")
    except KeyboardInterrupt:
        _out("\nstopped")
    return 0


# -- entrypoint ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livetennis",
        description="Command-line client for the Live Tennis API (https://livetennisapi.com)",
        epilog="The API key is read from --api-key or the LIVETENNISAPI_KEY environment variable.",
    )
    parser.add_argument("--version", action="version", version=f"livetennisapi {__version__}")
    parser.add_argument("--api-key", default=None, help="API key (default: $LIVETENNISAPI_KEY)")
    parser.add_argument("--base-url", default=None, help="override the API base URL")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of a table")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("health", help="liveness probe (no key needed)")
    p.set_defaults(func=_cmd_health)

    p = sub.add_parser("live", help="matches by status")
    p.add_argument("--status", default="live", choices=["live", "upcoming", "completed"])
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_cmd_live)

    p = sub.add_parser("match", help="full detail for one match")
    p.add_argument("match_id", type=int)
    p.set_defaults(func=_cmd_match)

    p = sub.add_parser("score", help="current score for one match")
    p.add_argument("match_id", type=int)
    p.set_defaults(func=_cmd_score)

    p = sub.add_parser("players", help="search players by name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=_cmd_players)

    p = sub.add_parser("fixtures", help="upcoming scheduled fixtures")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=_cmd_fixtures)

    p = sub.add_parser("history", help="recently completed matches")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=_cmd_history)

    p = sub.add_parser("watch", help="stream live scores over WebSocket (ULTRA)")
    p.add_argument("--match", type=int, default=None, help="watch one match instead of all")
    p.set_defaults(func=_cmd_watch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command != "health" and not (args.api_key or os.environ.get("LIVETENNISAPI_KEY")):
        _out("No API key. Set LIVETENNISAPI_KEY or pass --api-key.")
        _out("Get one at https://livetennisapi.com/#pricing")
        return 2

    client = LiveTennisAPI(args.api_key, base_url=args.base_url)
    try:
        return int(args.func(client, args) or 0)
    except Unauthorized:
        _out("Unauthorized — the key is missing, unknown, or disabled.")
        return 2
    except UpgradeRequired as exc:
        tier = exc.required_tier or "a higher"
        _out(f"This endpoint needs the {tier} tier. See https://livetennisapi.com/#pricing")
        return 3
    except RateLimited as exc:
        wait = f" Retry after {exc.retry_after:g}s." if exc.retry_after else ""
        _out(f"Rate limited.{wait}")
        return 4
    except LiveTennisAPIError as exc:
        _out(f"Error: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
