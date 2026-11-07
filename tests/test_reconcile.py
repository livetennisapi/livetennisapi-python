"""Frame parsing, point ordering and gap repair, and score withdrawals.

Everything here is pure: no socket, no HTTP. REST catch-up pages are plain
dicts shaped like ``GET /matches/{matchId}/points``.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from livetennisapi import LivePoint, PointsPage, PointUpdate, ScoreUpdate
from livetennisapi.reconcile import (
    ControlFrame,
    FrameError,
    Gap,
    PointSequencer,
    PointTagUpdate,
    ScoreBook,
    ScoreWithdrawn,
    parse_frame,
)

MATCH = 4711


def point_frame(seq: int, match_id: int = MATCH) -> dict:
    return {
        "type": "point",
        "match_id": match_id,
        "point": {"seq": seq, "set": 1, "game": 1, "number": seq, "winner": 1},
        "pbp_coverage": "point",
        "quality": "clean",
        "published_at": "2026-09-25T10:00:00.000Z",
    }


def page(*seqs: int, has_more: bool = False, basis: str = "live") -> dict:
    return {
        "match_id": MATCH,
        "pbp_coverage": "point",
        "quality": "clean",
        "basis": basis,
        "points": [{"seq": s, "set": 1, "game": 1, "number": s} for s in seqs],
        "last_seq": max(seqs) if seqs else 0,
        "has_more": has_more,
    }


def seqs(updates: list[PointUpdate]) -> list[int]:
    return [u.point.seq for u in updates]


def score_frame(sequence: int | None, match_id: int = MATCH, sets=(0, 0)) -> dict:
    return {"type": "score", "match_id": match_id, "score": {"sets": list(sets), "sequence": sequence}}


def withdrawn_frame(sequence: int, kind: str = "withdrawn", match_id: int = MATCH) -> dict:
    return {
        "type": "score_withdrawn",
        "match_id": match_id,
        "kind": kind,
        "superseded_sequence": sequence,
        "reason": "illegal_completed_set",
        "safe_to_resume": kind == "withdrawn",
        "ts": "2026-09-25T10:00:03.108Z",
        "published_at": "2026-09-25T10:00:03.115Z",
    }


# -- parse_frame ---------------------------------------------------------------


class TestParseFrame:
    def test_point_frame_from_text(self):
        frame = parse_frame(json.dumps(point_frame(7)))
        assert isinstance(frame, PointUpdate)
        assert isinstance(frame.point, LivePoint)
        assert frame.point.seq == 7
        assert frame.match_id == MATCH

    def test_score_frame_from_bytes_keeps_sequence(self):
        frame = parse_frame(json.dumps(score_frame(118, sets=(1, 0))).encode())
        assert isinstance(frame, ScoreUpdate)
        assert frame.score.sets == [1, 0]
        assert ScoreBook.sequence(frame) == 118

    def test_score_withdrawn_frame(self):
        frame = parse_frame(withdrawn_frame(118))
        assert isinstance(frame, ScoreWithdrawn)
        assert frame.superseded_sequence == 118
        assert frame.kind == "withdrawn"
        assert frame.safe_to_resume is True
        assert isinstance(frame.ts, datetime)
        assert isinstance(frame.published_at, datetime)

    def test_point_update_frame(self):
        frame = parse_frame({"type": "point_update", "match_id": MATCH, "seq": 9, "serve": 2, "outcome": "ace"})
        assert isinstance(frame, PointTagUpdate)
        assert (frame.seq, frame.serve, frame.outcome) == (9, 2, "ace")

    def test_control_and_unknown_frames(self):
        ack = parse_frame('{"type": "subscribed", "topics": ["live-scores"], "signals": ["points"]}')
        assert isinstance(ack, ControlFrame)
        assert ack.type == "subscribed"
        assert ack.signals == ["points"]
        future = parse_frame('{"type": "something_new", "x": 1}')
        assert isinstance(future, ControlFrame)
        assert future.type == "something_new"

    def test_deeply_nested_input_is_a_frame_error(self):
        with pytest.raises(FrameError):
            parse_frame("[" * 100_000)

    @pytest.mark.parametrize(
        "message",
        ["{not json", "", b"\xff\xfe", "[1, 2]", "42", '"score"', "{}", '{"type": 5}', '{"type": ""}', "null"],
    )
    def test_malformed_raises_frame_error(self, message):
        with pytest.raises(FrameError) as info:
            parse_frame(message)
        assert info.value.message == message
        assert isinstance(info.value, ValueError)


# -- PointSequencer ------------------------------------------------------------


class TestPointSequencer:
    def test_in_order_frames_pass_straight_through(self):
        sq = PointSequencer()
        out = []
        for s in (1, 2, 3, 4):
            out += sq.add(point_frame(s))
        assert seqs(out) == [1, 2, 3, 4]
        assert sq.cursor(MATCH) == 4
        assert sq.gap(MATCH) is None
        assert sq.gaps() == []

    def test_first_frame_mid_match_starts_the_cursor_there(self):
        sq = PointSequencer()
        assert seqs(sq.add(point_frame(40))) == [40]
        assert seqs(sq.add(point_frame(41))) == [41]

    def test_from_start_reports_the_points_before_the_first_frame(self):
        sq = PointSequencer(from_start=True)
        assert sq.add(point_frame(4)) == []
        assert sq.gap(MATCH) == Gap(match_id=MATCH, after_seq=0, before_seq=4)
        assert seqs(sq.fill(MATCH, page(1, 2, 3, 4))) == [1, 2, 3, 4]

    def test_out_of_order_frames_are_released_in_seq_order(self):
        sq = PointSequencer()
        assert seqs(sq.add(point_frame(1))) == [1]
        assert sq.add(point_frame(3)) == []
        assert sq.add(point_frame(4)) == []
        assert sq.held(MATCH) == 2
        assert seqs(sq.add(point_frame(2))) == [2, 3, 4]
        assert sq.held(MATCH) == 0
        assert sq.gap(MATCH) is None

    def test_duplicates_are_dropped(self):
        sq = PointSequencer()
        assert seqs(sq.add(point_frame(1))) == [1]
        assert sq.add(point_frame(1)) == []
        assert sq.add(point_frame(3)) == []
        assert sq.add(point_frame(3)) == []  # a repeat of a held frame
        assert seqs(sq.add(point_frame(2))) == [2, 3]
        assert sq.add(point_frame(2)) == []

    def test_gap_reports_the_range_and_rest_page_fills_it(self):
        sq = PointSequencer()
        for s in (1, 2, 3):
            sq.add(point_frame(s))
        assert sq.add(point_frame(7)) == []
        assert sq.add(point_frame(8)) == []

        gap = sq.gap(MATCH)
        assert gap == Gap(match_id=MATCH, after_seq=3, before_seq=7)
        assert list(gap.missing) == [4, 5, 6]
        assert sq.gaps() == [gap]

        # The catch-up read overlaps what we hold on both ends: 3 is already
        # released and 7, 8 are held. Each point still comes out exactly once.
        released = sq.fill(MATCH, page(3, 4, 5, 6, 7, 8))
        assert seqs(released) == [4, 5, 6, 7, 8]
        assert sq.gap(MATCH) is None
        assert sq.cursor(MATCH) == 8
        # A filled point carries the page's coverage, like a live frame.
        assert released[0].pbp_coverage == "point"
        assert released[0].raw["point"]["seq"] == 4

        # The live frames for the filled points turn up late: all repeats.
        for s in (4, 5, 6, 7, 8):
            assert sq.add(point_frame(s)) == []
        assert seqs(sq.add(point_frame(9))) == [9]

    def test_partial_fill_narrows_the_gap(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        sq.add(point_frame(6))
        assert seqs(sq.fill(MATCH, page(2, 3, has_more=True))) == [2, 3]
        assert sq.gap(MATCH) == Gap(match_id=MATCH, after_seq=3, before_seq=6)
        assert seqs(sq.fill(MATCH, page(4, 5))) == [4, 5, 6]

    def test_fill_accepts_a_points_page_or_a_list(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        sq.add(point_frame(4))
        parsed = PointsPage.from_dict(page(2))
        assert seqs(sq.fill(MATCH, parsed)) == [2]
        assert seqs(sq.fill(MATCH, [LivePoint.from_dict({"seq": 3})])) == [3, 4]

    def test_fill_sorts_an_unsorted_page(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        assert seqs(sq.fill(MATCH, page(4, 2, 3))) == [2, 3, 4]

    def test_fill_accepts_point_dicts(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        assert seqs(sq.fill(MATCH, [{"seq": 2}, {"seq": 3}])) == [2, 3]

    def test_fill_refuses_a_page_for_another_match(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        other = page(2)
        other["match_id"] = MATCH + 1
        with pytest.raises(ValueError, match="not match"):
            sq.fill(MATCH, other)
        assert sq.cursor(MATCH) == 1

    def test_from_start_keeps_out_of_order_first_frames(self):
        sq = PointSequencer(from_start=True)
        sq.resume(MATCH, 4)
        assert sq.add(point_frame(6)) == []
        assert seqs(sq.add(point_frame(5))) == [5, 6]

    def test_fill_refuses_the_reconstruction_basis(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        with pytest.raises(ValueError, match="reconstruction"):
            sq.fill(MATCH, page(1, 2, basis="reconstruction"))

    def test_matches_are_tracked_separately(self):
        sq = PointSequencer()
        sq.add(point_frame(1, match_id=1))
        sq.add(point_frame(10, match_id=2))
        assert sq.add(point_frame(3, match_id=1)) == []
        assert seqs(sq.add(point_frame(11, match_id=2))) == [11]
        assert sq.gaps() == [Gap(match_id=1, after_seq=1, before_seq=3)]
        assert sq.cursors == {1: 1, 2: 11}

    def test_resume_sets_the_cursor_and_releases_what_follows(self):
        sq = PointSequencer()
        sq.resume(MATCH, 10)
        assert sq.add(point_frame(9)) == []
        assert sq.add(point_frame(12)) == []
        assert sq.gap(MATCH) == Gap(match_id=MATCH, after_seq=10, before_seq=12)
        # Learning elsewhere that 11 was already handled closes the gap.
        assert seqs(sq.resume(MATCH, 11)) == [12]
        with pytest.raises(ValueError):
            sq.resume(MATCH, -1)

    def test_resume_never_moves_the_cursor_back(self):
        sq = PointSequencer()
        for s in (1, 2, 3):
            sq.add(point_frame(s))
        with pytest.raises(ValueError, match="behind"):
            sq.resume(MATCH, 1)
        assert sq.fill(MATCH, page(2, 3)) == []
        sq.forget(MATCH)
        assert sq.resume(MATCH, 1) == []

    def test_forget_clears_a_match(self):
        sq = PointSequencer()
        sq.add(point_frame(1))
        sq.add(point_frame(5))
        sq.forget(MATCH)
        assert sq.cursor(MATCH) is None
        assert sq.gaps() == []

    def test_unorderable_frames_are_returned_not_dropped(self):
        sq = PointSequencer()
        no_seq = {"type": "point", "match_id": MATCH, "point": {"set": 1}}
        bool_seq = {"type": "point", "match_id": MATCH, "point": {"seq": True}}
        no_match = {"type": "point", "point": {"seq": 3}}
        for frame in (no_seq, bool_seq, no_match):
            out = sq.add(frame)
            assert len(out) == 1
        assert sq.cursors == {}


# -- ScoreBook -----------------------------------------------------------------


class TestScoreBook:
    def test_newer_scores_replace_older_and_stale_ones_are_ignored(self):
        book = ScoreBook()
        assert book.apply(parse_frame(score_frame(5))) is True
        assert book.apply(parse_frame(score_frame(7, sets=(1, 0)))) is True
        assert book.apply(parse_frame(score_frame(6))) is False
        assert book.apply(parse_frame(score_frame(7, sets=(1, 0)))) is False
        assert ScoreBook.sequence(book.get(MATCH)) == 7

    def test_withdrawn_discards_the_held_state(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(118)))
        assert book.apply(parse_frame(withdrawn_frame(118))) is True
        assert book.get(MATCH) is None
        assert book.verdict(MATCH).superseded_sequence == 118
        # The taken-back state is never shown again, even if re-sent.
        assert book.apply(parse_frame(score_frame(118))) is False
        # The next accepted state is the one to resume from, and clears the verdict.
        assert book.apply(parse_frame(score_frame(119))) is True
        assert book.verdict(MATCH) is None

    def test_an_older_state_reserved_after_a_withdrawal_is_shown(self):
        # GET /matches/{id}/score re-serves the last good state, whose sequence is lower.
        book = ScoreBook()
        book.apply(parse_frame(score_frame(4)))
        book.apply(parse_frame(score_frame(5)))
        book.apply(parse_frame(withdrawn_frame(5)))
        assert book.apply(parse_frame(score_frame(4))) is True
        assert ScoreBook.sequence(book.get(MATCH)) == 4

    def test_withdrawal_that_arrives_before_its_score_still_refuses_it(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(5)))
        assert book.apply(parse_frame(withdrawn_frame(6))) is False
        assert book.apply(parse_frame(score_frame(6))) is False
        assert ScoreBook.sequence(book.get(MATCH)) == 5
        assert book.apply(parse_frame(score_frame(7))) is True

    def test_withheld_discards_the_held_state(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(40)))
        assert book.apply(parse_frame(withdrawn_frame(40, kind="withheld"))) is True
        assert book.get(MATCH) is None

    def test_deferred_keeps_the_state_and_flags_it(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(40)))
        assert book.apply(parse_frame(withdrawn_frame(40, kind="deferred"))) is False
        assert ScoreBook.sequence(book.get(MATCH)) == 40
        assert book.verdict(MATCH).kind == "deferred"
        book.apply(parse_frame(score_frame(41)))
        assert book.verdict(MATCH) is None

    def test_withdrawal_of_a_state_not_held_changes_nothing(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(50)))
        assert book.apply(parse_frame(withdrawn_frame(49))) is False
        assert book.apply(parse_frame(withdrawn_frame(50, match_id=999))) is False
        assert ScoreBook.sequence(book.get(MATCH)) == 50
        assert book.verdict(MATCH) is None

    def test_withdrawal_without_a_sequence_changes_nothing(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(None)))
        frame = withdrawn_frame(1)
        del frame["superseded_sequence"]
        assert book.apply(parse_frame(frame)) is False
        assert book.get(MATCH) is not None

    def test_unknown_kind_keeps_the_state_and_flags_it(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(8)))
        assert book.apply(parse_frame(withdrawn_frame(8, kind="something_new"))) is False
        assert ScoreBook.sequence(book.get(MATCH)) == 8
        assert book.verdict(MATCH).kind == "something_new"

    def test_other_frames_are_ignored(self):
        book = ScoreBook()
        assert book.apply(parse_frame(point_frame(1))) is False
        assert book.apply(parse_frame('{"type": "ping"}')) is False

    def test_forget_clears_a_match(self):
        book = ScoreBook()
        book.apply(parse_frame(score_frame(3)))
        book.apply(parse_frame(withdrawn_frame(3)))
        book.forget(MATCH)
        assert book.verdict(MATCH) is None
        assert book.apply(parse_frame(score_frame(1))) is True
