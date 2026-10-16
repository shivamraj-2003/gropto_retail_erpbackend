"""Transfers that do not arrive exactly as they left. No database needed."""

from app.services.transfer_math import (
    LineQty,
    closed_status,
    open_qty,
    plan_resolution,
    status_after_delivery,
)


def test_sent_five_received_four_is_one_open():
    line = LineQty(dispatched=5, received=4)
    assert open_qty(line) == 1
    assert status_after_delivery([line], close=True) == "discrepancy"


def test_exact_receipt_closes_cleanly():
    assert status_after_delivery([LineQty(5, received=5)], close=False) == "received"
    assert closed_status([LineQty(5, received=5), LineQty(2, received=2)]) == "received"


def test_partial_delivery_waits_for_the_rest():
    lines = [LineQty(5, received=3)]
    assert status_after_delivery(lines, close=False) == "partially_received"
    # the rest arrives later
    assert status_after_delivery([LineQty(5, received=5)], close=False) == "received"
    # or the receiver says nothing more is coming
    assert status_after_delivery(lines, close=True) == "discrepancy"


def test_nothing_counted_yet_stays_on_the_way():
    assert status_after_delivery([LineQty(5)], close=False) == "dispatched"


def test_damaged_units_count_as_arrived_but_need_checking():
    line = LineQty(5, received=4, damaged=1)
    assert open_qty(line) == 0
    assert status_after_delivery([line], close=False) == "discrepancy"


def test_more_than_sent_is_flagged():
    line = LineQty(5, received=6)
    assert open_qty(line) == -1
    assert status_after_delivery([line], close=False) == "discrepancy"


def test_one_bad_line_among_good_ones():
    lines = [LineQty(10, received=10), LineQty(5, received=4), LineQty(3, received=3)]
    assert closed_status(lines) == "discrepancy"


def test_missing_goods_lost_are_written_off_without_moving_stock():
    steps, errors = plan_resolution({"a": LineQty(5, received=4)}, {"a": "write_off"})
    assert errors == []
    assert [(s.action, s.qty, s.place, s.delta) for s in steps] == [("write_off", 1, None, 0.0)]


def test_missing_goods_that_never_left_go_back_to_the_sender():
    steps, errors = plan_resolution({"a": LineQty(5, received=4)}, {"a": "return_to_source"})
    assert errors == []
    assert [(s.place, s.delta) for s in steps] == [("source", 1)]


def test_extra_goods_either_correct_the_sender_or_go_back():
    lines = {"a": LineQty(5, received=6)}
    steps, _ = plan_resolution(lines, {"a": "reduce_sender"})
    assert [(s.place, s.delta) for s in steps] == [("source", -1)]
    steps, _ = plan_resolution(lines, {"a": "send_back"})
    assert [(s.place, s.delta) for s in steps] == [("dest", -1)]


def test_a_line_that_needs_an_action_must_get_a_valid_one():
    _, errors = plan_resolution({"a": LineQty(5, received=4), "b": LineQty(5, received=6)}, {})
    assert errors == ["a", "b"]
    # an action for the wrong kind of mismatch is refused too
    _, errors = plan_resolution({"a": LineQty(5, received=4)}, {"a": "send_back"})
    assert errors == ["a"]
    _, errors = plan_resolution({"b": LineQty(5, received=6)}, {"b": "write_off"})
    assert errors == ["b"]


def test_damaged_only_needs_no_action_and_moves_no_stock():
    steps, errors = plan_resolution({"a": LineQty(5, received=4, damaged=1)}, {})
    assert (steps, errors) == ([], [])


def test_exact_lines_are_left_alone():
    steps, errors = plan_resolution({"a": LineQty(5, received=5), "b": LineQty(2, received=1)}, {"b": "write_off"})
    assert errors == []
    assert [s.item_id for s in steps] == ["b"]
