"""Per-store e-invoice rule (no database)."""

from types import SimpleNamespace

from app.services.gst import DEFAULT_AATO_THRESHOLD, current_financial_year, decide_required, store_threshold
from datetime import date

CRORE = 1_00_00_000


def test_default_threshold_is_five_crore():
    assert DEFAULT_AATO_THRESHOLD == 5 * CRORE
    assert store_threshold(SimpleNamespace(aato_threshold=None)) == 5 * CRORE


def test_store_own_threshold_overrides_default():
    assert store_threshold(SimpleNamespace(aato_threshold=2 * CRORE)) == 2 * CRORE


def test_required_only_when_last_year_passed_threshold_or_flagged():
    t = 5 * CRORE
    assert not decide_required(flagged=False, previous_year_turnover=4.99 * CRORE, threshold=t)
    assert not decide_required(flagged=False, previous_year_turnover=t, threshold=t)  # must exceed, not equal
    assert decide_required(flagged=False, previous_year_turnover=5.01 * CRORE, threshold=t)
    assert decide_required(flagged=True, previous_year_turnover=0, threshold=t)


def test_each_store_judged_alone():
    # Three stores of ₹3 crore each = ₹9 crore together, but none passes ₹5 crore by itself.
    stores = [3 * CRORE, 3 * CRORE, 3 * CRORE]
    assert [decide_required(flagged=False, previous_year_turnover=s, threshold=5 * CRORE) for s in stores] == [False] * 3


def test_financial_year_runs_april_to_march():
    assert current_financial_year(date(2026, 3, 31)) == 2025
    assert current_financial_year(date(2026, 4, 1)) == 2026
    assert current_financial_year(date(2026, 10, 7)) == 2026
