"""Model behaviour — above all, forward compatibility.

The API ships additive changes within v1, so the single most important property
of these models is that a field they have never heard of does not break them.
"""

from datetime import date, datetime

import pytest

from livetennisapi.models import (
    ArchiveMatch,
    ArchiveParticipant,
    ArchivePlayerBio,
    ChartingMatch,
    ChartingPlayer,
    Fixture,
    HeadToHead,
    HistoryCoverage,
    HistoryPackage,
    HistoryTape,
    LivePoint,
    Market,
    Match,
    MatchStatistics,
    Page,
    Player,
    PointsPage,
    RallyMatch,
    RallyPoint,
    RankingRecord,
    Score,
    TapeRow,
    Tournament,
    Usage,
    WSToken,
)


class TestForwardCompatibility:
    """A new server field must never break an old client."""

    def test_unknown_fields_do_not_raise(self):
        match = Match.from_dict(
            {"id": 1, "tournament": "ATP Wimbledon", "a_field_invented_next_year": {"nested": True}}
        )
        assert match.id == 1
        assert match.tournament == "ATP Wimbledon"

    def test_unknown_fields_are_readable_as_attributes(self):
        match = Match.from_dict({"id": 1, "future_field": "hello"})
        assert match.future_field == "hello"

    def test_unknown_fields_are_in_raw(self):
        match = Match.from_dict({"id": 1, "future_field": "hello"})
        assert match.raw["future_field"] == "hello"

    def test_raw_preserves_the_exact_payload(self):
        payload = {"id": 1, "tournament": "X", "extra": [1, 2, 3]}
        assert Match.from_dict(payload).to_dict() == payload

    def test_missing_fields_become_none(self):
        match = Match.from_dict({"id": 1})
        assert match.tournament is None
        assert match.score is None

    def test_genuinely_absent_attribute_still_raises(self):
        """Forward compatibility must not turn every typo into a silent None."""
        match = Match.from_dict({"id": 1})
        with pytest.raises(AttributeError, match="definitely_not_a_field"):
            _ = match.definitely_not_a_field

    def test_none_in_none_out(self):
        assert Match.from_dict(None) is None

    def test_unexpected_type_is_preserved_not_coerced(self):
        # The server sent a list where an object was documented.
        match = Match.from_dict(["not", "an", "object"])
        assert match is not None
        assert match.raw["_unexpected"] == ["not", "an", "object"]


class TestScore:
    """`games` is player-major. This is the API's sharpest edge."""

    def test_games_is_player_major(self):
        # 6-4, 3-6, 2-1
        score = Score.from_dict({"games": [[6, 3, 2], [4, 6, 1]], "sets": [1, 1]})
        assert score.games_for_set(0) == (6, 4)
        assert score.games_for_set(1) == (3, 6)
        assert score.games_for_set(2) == (2, 1)

    def test_games_for_set_out_of_range(self):
        score = Score.from_dict({"games": [[6], [4]]})
        assert score.games_for_set(5) == (None, None)

    def test_games_for_set_with_no_games(self):
        assert Score.from_dict({}).games_for_set(0) == (None, None)

    def test_ragged_games_do_not_raise(self):
        # A set in progress can leave the two sides different lengths.
        score = Score.from_dict({"games": [[6, 3, 2], [4, 6]]})
        assert score.games_for_set(2) == (2, None)

    def test_timestamp_is_parsed(self):
        score = Score.from_dict({"timestamp": "2026-07-18T14:30:00Z"})
        assert isinstance(score.timestamp, datetime)
        assert score.timestamp.year == 2026

    def test_unparseable_timestamp_is_left_alone(self):
        score = Score.from_dict({"timestamp": "not a timestamp"})
        assert score.timestamp == "not a timestamp"

    def test_model_fields_absent_below_ultra(self):
        score = Score.from_dict({"sets": [1, 0]})
        assert score.win_probability_p1 is None
        assert score.danger is None


class TestMatch:
    def test_nested_players_become_models(self):
        match = Match.from_dict(
            {"id": 1, "players": {"p1": {"id": 10, "name": "A"}, "p2": {"id": 11, "name": "B"}}}
        )
        assert isinstance(match.p1, Player)
        assert match.p1.name == "A"
        assert match.p2.name == "B"

    def test_missing_players_object(self):
        match = Match.from_dict({"id": 1})
        assert match.p1 is None
        assert match.p2 is None

    def test_nested_score_becomes_a_model(self):
        match = Match.from_dict({"id": 1, "score": {"sets": [1, 0]}})
        assert isinstance(match.score, Score)
        assert match.score.sets == [1, 0]

    def test_market_and_analysis_absent_below_tier(self):
        match = Match.from_dict({"id": 1})
        assert match.market is None
        assert match.analysis is None

    def test_scheduled_time_is_parsed(self):
        match = Match.from_dict({"id": 1, "scheduled_time": "2026-07-18T14:30:00Z"})
        assert isinstance(match.scheduled_time, datetime)


class TestMarket:
    def test_prices_become_models(self):
        market = Market.from_dict(
            {"id": 1, "prices": [{"side": 1, "mid": 0.62}, {"side": 2, "mid": 0.38}]}
        )
        assert len(market.prices) == 2
        assert market.prices[0].mid == 0.62

    def test_missing_prices_is_an_empty_list(self):
        assert Market.from_dict({"id": 1}).prices == []


class TestPlayer:
    def test_birthday_is_parsed_as_a_date(self):
        player = Player.from_dict({"id": 1, "birthday": "1987-05-22"})
        assert isinstance(player.birthday, date)
        assert player.birthday.year == 1987

    def test_null_birthday(self):
        assert Player.from_dict({"id": 1, "birthday": None}).birthday is None


class TestFixture:
    def test_event_date_is_parsed(self):
        fixture = Fixture.from_dict({"id": 1, "event_date": "2026-07-20"})
        assert isinstance(fixture.event_date, date)

    def test_start_time_is_parsed_and_nullable(self):
        # A date-only fixture is a real state: start_time stays None until the
        # order of play assigns a time.
        fixture = Fixture.from_dict({"id": 1, "start_time": "2026-08-03T11:00:00Z"})
        assert isinstance(fixture.start_time, datetime)
        assert Fixture.from_dict({"id": 1, "start_time": None}).start_time is None

    def test_player_ids_and_round_code(self):
        fixture = Fixture.from_dict({"id": 1, "player1_id": 925, "player2_id": None, "round_code": "QF"})
        assert fixture.player1_id == 925
        assert fixture.player2_id is None
        assert fixture.round_code == "QF"


class TestMatchNewFields:
    def test_tour_and_tournament_id(self):
        match = Match.from_dict({"id": 1, "tour": "itf", "tournament_id": "itf-m15-wuning"})
        assert match.tour == "itf"
        assert match.tournament_id == "itf-m15-wuning"

    def test_round_code_and_withdrew(self):
        match = Match.from_dict(
            {"id": 1, "round_code": "R16", "event_status": "Retired", "winner": 2, "withdrew": 1}
        )
        assert match.round_code == "R16"
        assert match.withdrew == 1

    def test_withdrew_absent_means_none(self):
        # Absent means "not a withdrawal, or no evidence" — never a guess.
        assert Match.from_dict({"id": 1}).withdrew is None

    def test_event_status_updated_at_is_parsed(self):
        # The instant the current event_status was recorded (added 2026-08-19).
        match = Match.from_dict(
            {"id": 1, "event_status": "Walk Over", "event_status_updated_at": "2026-08-19T09:15:00Z"}
        )
        assert isinstance(match.event_status_updated_at, datetime)
        assert match.event_status_updated_at.tzinfo is not None

    def test_event_status_updated_at_is_never_backfilled(self):
        # Null (or absent — every match from before the field existed) stays None.
        assert Match.from_dict({"id": 1}).event_status_updated_at is None
        assert Match.from_dict({"id": 1, "event_status_updated_at": None}).event_status_updated_at is None

    def test_draw_is_three_valued(self):
        assert Match.from_dict({"id": 1, "draw": "singles"}).draw == "singles"
        assert Match.from_dict({"id": 1, "draw": "doubles"}).draw == "doubles"
        # Null is an answer — the draw is UNKNOWN (team ties, team
        # exhibitions), never coerced to singles.
        assert Match.from_dict({"id": 1, "draw": None}).draw is None
        assert Match.from_dict({"id": 1}).draw is None

    def test_draw_rides_beside_the_lossy_is_doubles(self):
        match = Match.from_dict({"id": 1, "draw": "doubles", "is_doubles": True})
        assert match.draw == "doubles"
        assert match.is_doubles is True


class TestTournament:
    def test_fields(self):
        t = Tournament.from_dict(
            {"id": "atp-wimbledon", "name": "Wimbledon", "tour": "atp", "surface": "grass",
             "indoor": False, "city": "London", "country": "GB", "category": "grand_slam"}
        )
        assert t.id == "atp-wimbledon"
        assert t.category == "grand_slam"

    def test_uncurated_location_is_none(self):
        t = Tournament.from_dict({"id": "x", "name": "Y", "city": None, "country": None, "category": None})
        assert t.city is None
        assert t.category is None


class TestArchiveMatch:
    def test_winner_and_loser_become_participants(self):
        m = ArchiveMatch.from_dict(
            {
                "id": 1, "tour": "atp", "event_date": "1980-06-23", "round": "F",
                "winner": {"name": "Bjorn Borg", "rank": 1, "player_id": 100437, "entry": None},
                "loser": {"name": "John McEnroe", "rank": 2, "player_id": 100581, "seed": 2},
                "score": "1-6 7-5 6-3 6-7(16) 8-6", "outcome": "completed",
            }
        )
        assert isinstance(m.winner, ArchiveParticipant)
        assert m.winner.name == "Bjorn Borg"
        assert m.loser.player_id == 100581
        assert isinstance(m.event_date, date)

    def test_pre_1991_stats_stay_none(self):
        # The corpus records serve stats from 1991; the None is honest.
        m = ArchiveMatch.from_dict({"id": 1, "event_date": "1975-01-01", "stats": None})
        assert m.stats is None


class TestArchivePlayerBio:
    def test_dates_are_parsed(self):
        bio = ArchivePlayerBio.from_dict(
            {"id": 100437, "tour": "atp", "name": "Bjorn Borg", "dob": "1956-06-06",
             "career_high_rank": 1, "career_high_date": "1977-08-23"}
        )
        assert isinstance(bio.dob, date)
        assert isinstance(bio.career_high_date, date)
        assert bio.career_high_rank == 1

    def test_the_eras_silence_is_none(self):
        bio = ArchivePlayerBio.from_dict({"id": 1, "tour": "wta", "hand": None, "height_cm": None})
        assert bio.hand is None
        assert bio.height_cm is None


class TestHeadToHead:
    def test_shape(self):
        h2h = HeadToHead.from_dict(
            {
                "players": {"p1": {"name": "Roger Federer"}, "p2": {"name": "Rafael Nadal"}},
                "totals": {"p1_wins": 16, "p2_wins": 24, "meetings": 40, "undecided": 0},
                "by_surface": {"clay": {"p1": 2, "p2": 14}},
                "meetings": [{"era": "current", "match_id": 18453, "winner": 2, "outcome": "completed"}],
            }
        )
        assert h2h.totals["p2_wins"] == 24
        assert h2h.meetings[0]["era"] == "current"

    def test_no_match_is_an_empty_record_not_a_crash(self):
        h2h = HeadToHead.from_dict({"players": None, "totals": {"meetings": 0}})
        assert h2h.players is None
        assert h2h.meetings == []


class TestHistoryTape:
    def test_tape_rows_are_scores_with_point_winner(self):
        tape = HistoryTape.from_dict(
            {
                "match": {"id": 18953, "tour": "atp"},
                "tape": [
                    {"sets": [0, 0], "games": [[0], [0]], "timestamp": "2026-08-01T10:00:00Z"},
                    {"sets": [0, 0], "games": [[1], [0]], "point_winner": 1},
                ],
                "meta": {"match_id": 18953, "rows": 2, "coverage": "from_start",
                         "point_source": "observed", "sequence": "clean"},
            }
        )
        assert isinstance(tape.match, Match)
        assert isinstance(tape.tape[0], TapeRow)
        assert isinstance(tape.tape[0], Score)  # a TapeRow is a Score
        assert tape.tape[0].games_for_set(0) == (0, 0)
        assert tape.tape[0].point_winner is None  # first row: never attributable
        assert tape.tape[1].point_winner == 1
        assert tape.meta.coverage == "from_start"
        assert tape.meta.point_source == "observed"

    def test_reconstructed_row_nulls_are_preserved(self):
        # Null timestamp is THE row-level marker of a reconstructed row.
        row = TapeRow.from_dict(
            {"sets": [1, 0], "timestamp": None, "win_probability_p1": None, "danger": None}
        )
        assert row.timestamp is None
        assert row.win_probability_p1 is None

    def test_per_set_tiebreaks(self):
        tape = HistoryTape.from_dict(
            {"tape": [], "tiebreaks": [None, {"p1": 7, "p2": 5}]}
        )
        assert tape.tiebreaks[0] is None  # set 1 was not a breaker (or unobserved)
        assert tape.tiebreaks[1] == {"p1": 7, "p2": 5}

    def test_no_seven_six_set_means_null_tiebreaks(self):
        assert HistoryTape.from_dict({"tape": [], "tiebreaks": None}).tiebreaks is None


class TestMatchStatistics:
    def test_families_and_freshness(self):
        stats = MatchStatistics.from_dict(
            {
                "match_id": 18953,
                "coverage": "live",
                "games_counted": 18,
                "tiebreak_games_excluded": 1,
                "freshness": {
                    "derived": {"coverage": "live", "age_seconds": 0},
                    "measured": {"coverage": "stale", "age_seconds": 95},
                    "measured_divergence": None,
                },
                "players": {
                    "p1": {"hold_pct": 89, "measured": {"aces": 11, "double_faults": 2}},
                    "p2": {"hold_pct": 78, "measured": {"aces": 3}},
                },
            }
        )
        assert stats.p1["hold_pct"] == 89
        assert stats.p1["measured"]["aces"] == 11
        # Measured fields are OMITTED when absent, never zero-filled.
        assert "double_faults" not in stats.p2["measured"]
        assert stats.freshness["measured"]["coverage"] == "stale"

    def test_nothing_held_is_null_players_not_a_crash(self):
        stats = MatchStatistics.from_dict({"match_id": 1, "coverage": "none", "players": None})
        assert stats.p1 is None
        assert stats.p2 is None


class TestRankingRecord:
    def test_rank_systems_with_previous_rank(self):
        rec = RankingRecord.from_dict(
            {"player_id": 925, "system": "atp", "rank": 3, "points": 6030,
             "previous_rank": 4, "effective_date": "2026-08-03",
             "observed_at": "2026-08-03T09:00:00Z"}
        )
        assert rec.previous_rank == 4
        assert isinstance(rec.effective_date, date)
        assert isinstance(rec.observed_at, datetime)

    def test_utr_is_a_rating_not_a_rank(self):
        rec = RankingRecord.from_dict({"player_id": 925, "system": "utr", "rank": None,
                                       "points": None, "rating": 15.83, "previous_rank": None})
        assert rec.rank is None
        assert rec.rating == 15.83

    def test_listing_rows_carry_the_published_name(self):
        # Off-roster players keep their published name with a null id — no silent holes.
        rec = RankingRecord.from_dict({"player_id": None, "player_name": "A. Nobody", "system": "wta", "rank": 731})
        assert rec.player_id is None
        assert rec.player_name == "A. Nobody"


class TestLivePoints:
    def test_live_point_parses_every_field(self):
        p = LivePoint.from_dict(
            {"seq": 41, "set": 2, "game": 5, "number": 3, "tiebreak": False,
             "server": 1, "winner": 2, "score": {"p1": "30", "p2": "40"},
             "sets": [1, 0], "games": [[6, 2], [4, 3]], "ts": "2026-08-17T10:15:03Z"}
        )
        assert p.seq == 41
        assert p.set == 2
        assert p.winner == 2
        assert p.score == {"p1": "30", "p2": "40"}
        assert p.games == [[6, 2], [4, 3]]
        assert isinstance(p.ts, datetime)

    def test_unstated_server_and_winner_stay_none(self):
        p = LivePoint.from_dict({"seq": 1, "server": None, "winner": None})
        assert p.server is None
        assert p.winner is None

    def test_points_page_parses_and_iterates(self):
        page = PointsPage.from_dict(
            {"match_id": 18953, "pbp_coverage": "point", "quality": "clean",
             "covers_from_start": True,
             "points": [{"seq": 1, "winner": 1}, {"seq": 2, "winner": 2}],
             "last_seq": 2, "has_more": True}
        )
        assert page.match_id == 18953
        assert page.covers_from_start is True
        assert page.last_seq == 2
        assert page.has_more is True
        assert len(page) == 2
        assert [p.seq for p in page] == [1, 2]
        assert all(isinstance(p, LivePoint) for p in page)

    def test_absent_covers_from_start_is_none(self):
        # Older servers omit the field entirely: not measured, never "no".
        page = PointsPage.from_dict({"match_id": 1, "points": [], "last_seq": None, "has_more": False})
        assert page.covers_from_start is None

    def test_null_points_reads_as_an_empty_page(self):
        # A null (or garbage) ``points`` must normalize to [], never leak a
        # None that crashes the page iterators or the push resume.
        page = PointsPage.from_dict({"match_id": 1, "points": None, "last_seq": None, "has_more": False})
        assert page.points == []
        assert len(page) == 0
        assert list(page) == []


class TestRally:
    def test_rally_match_with_points(self):
        m = RallyMatch.from_dict(
            {
                "rally_match_id": 4242,
                "match_id": None,
                "date": "2008-07-06",
                "players": [{"name": "Roger Federer", "hand": "R"}, {"name": "Rafael Nadal", "hand": "L"}],
                "points": 413,
                "points_parsed": 409,
                "rally": [
                    {"point": 1, "server": 1, "point_winner": 2, "raw": "4d;236b2f1*", "parsed": True,
                     "rally_length": 3, "is_ace": False, "serve_direction": "wide"},
                ],
                "meta": {"limit": 50, "offset": 0, "count": 1, "total": 413},
            }
        )
        assert m.match_id is None  # most charted matches predate our collection
        assert isinstance(m.date, date)
        assert isinstance(m.rally[0], RallyPoint)
        assert m.rally[0].point_winner == 2
        assert m.meta.total == 413

    def test_notation_survives_the_raw_name_collision(self):
        """The wire field is `raw`, which every model already uses for its
        payload — the shot string must stay reachable via `.notation`."""
        p = RallyPoint.from_dict({"point": 1, "raw": "6f28b3*", "parsed": True})
        assert p.notation == "6f28b3*"
        assert p.raw == {"point": 1, "raw": "6f28b3*", "parsed": True}

    def test_unparsed_point_keeps_the_recognised_part(self):
        p = RallyPoint.from_dict({"point": 9, "raw": "??", "parsed": False, "serve_number": None})
        assert p.parsed is False
        assert p.notation == "??"


class TestCharting:
    def test_player_aggregate(self):
        cp = ChartingPlayer.from_dict(
            {"player": {"name": "Pete Sampras", "gender": "men"}, "matches_charted": 361,
             "coverage": "curated", "families": {"serve_placement": {"deuce_wide": 1204}}}
        )
        assert cp.matches_charted == 361
        assert cp.families["serve_placement"]["deuce_wide"] == 1204

    def test_charted_match(self):
        cm = ChartingMatch.from_dict(
            {"charting_match_id": 777, "mcp_id": "20080706-M-Wimbledon-F", "gender": "M",
             "players": {"p1": "Roger Federer", "p2": "Rafael Nadal"},
             "families": {"overview": {"1": {}, "2": {}, "Total": {}}}}
        )
        assert cm.charting_match_id == 777
        assert "Total" in cm.families["overview"]


class TestHistoryPackage:
    def test_manifest(self):
        pkg = HistoryPackage.from_dict(
            {"period": "2026-07", "status": "ready", "match_count": 4211, "row_count": 801532,
             "files": [{"format": "jsonl", "filename": "2026-07.jsonl.gz", "bytes": 1024, "sha256": "ab"}],
             "built_at": "2026-08-01T02:00:00Z"}
        )
        assert pkg.period == "2026-07"
        assert pkg.kind is None  # absent on tape packages, so old parsers see no change
        assert isinstance(pkg.built_at, datetime)
        assert pkg.files[0]["format"] == "jsonl"

    def test_rankings_package_counts_players_and_records(self):
        pkg = HistoryPackage.from_dict({"period": "2026-07", "kind": "rankings", "match_count": 2100})
        assert pkg.kind == "rankings"


class TestHistoryCoverage:
    def test_shape_and_as_of(self):
        cov = HistoryCoverage.from_dict(
            {"as_of": "2026-08-18T04:15:00Z", "method": "ledger",
             "buckets": {"atp_singles": {"completed": 9000, "any_tape": 8900,
                                         "point_complete": 6200,
                                         "complete_on_default_read": 6000, "share": 0.689}},
             "totals": {"completed": 9000, "any_tape": 8900, "point_complete": 6200,
                        "complete_on_default_read": 6000, "share": 0.689}}
        )
        assert isinstance(cov.as_of, datetime)
        assert cov.method == "ledger"
        assert cov.buckets["atp_singles"]["share"] == 0.689
        assert cov.totals["point_complete"] == 6200


class TestWSToken:
    def test_channels(self):
        tok = WSToken.from_dict(
            {"token": "eyJ…", "expires_in": 300,
             "ws_url": "wss://api.livetennisapi.com/connection/websocket",
             "channels": {"match": "match:{id}", "slate": "slate:all"}}
        )
        assert tok.slate_channel == "slate:all"
        assert tok.match_channel(18953) == "match:18953"

    def test_channel_helpers_survive_missing_channels(self):
        tok = WSToken.from_dict({"token": "t"})
        assert tok.slate_channel is None
        assert tok.match_channel(1) is None

    def test_point_channels_read_from_the_vocabulary(self):
        tok = WSToken.from_dict(
            {"token": "t",
             "channels": {"match": "match:{id}", "slate": "slate:all",
                          "point_match": "point:match:{id}", "point_slate": "point:slate"}}
        )
        assert tok.point_slate_channel == "point:slate"
        assert tok.point_match_channel(18953) == "point:match:18953"

    def test_absent_point_vocabulary_is_an_honest_none(self):
        # A mint without the point family means this key will not receive
        # point frames — the helpers must refuse to guess a name.
        tok = WSToken.from_dict(
            {"token": "t", "channels": {"match": "match:{id}", "slate": "slate:all"}}
        )
        assert tok.point_slate_channel is None
        assert tok.point_match_channel(18953) is None

    def test_signal_channels_read_from_the_vocabulary(self):
        tok = WSToken.from_dict(
            {"token": "t",
             "channels": {"match": "match:{id}", "slate": "slate:all",
                          "signal_match": "signal:match:{match_id}",
                          "signal_slate": "signal:slate"}}
        )
        assert tok.signal_slate_channel == "signal:slate"
        assert tok.signal_match_channel(18953) == "signal:match:18953"

    def test_absent_signal_vocabulary_is_an_honest_none(self):
        # Same honest refusal as the point helpers: no advertised family,
        # no guessed name.
        tok = WSToken.from_dict(
            {"token": "t", "channels": {"match": "match:{id}", "slate": "slate:all"}}
        )
        assert tok.signal_slate_channel is None
        assert tok.signal_match_channel(18953) is None


class TestUsage:
    def test_shape(self):
        usage = Usage.from_dict(
            {"tier": "free", "base_tier": "free", "channel": "direct",
             "limits": {"per_minute": 30, "per_day": 100},
             "today": {"calls": 41, "errors": 0, "remaining_day": 59},
             "history": [{"day": "2026-08-06", "calls": 100, "errors": 2}],
             "as_of": "2026-08-07T10:00:00Z"}
        )
        assert usage.limits["per_day"] == 100
        assert usage.today["remaining_day"] == 59
        assert isinstance(usage.as_of, datetime)


class TestPage:
    def test_page_is_iterable_and_sized(self):
        page = Page(data=[1, 2, 3])
        assert len(page) == 3
        assert list(page) == [1, 2, 3]
        assert page[0] == 1
        assert page.count == 3
