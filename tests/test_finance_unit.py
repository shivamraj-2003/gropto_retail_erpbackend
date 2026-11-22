from app.api.v1.finance_vouchers import account_key
from app.api.v1.ledger import Book, _signed


def test_known_account_keys_are_kept_and_new_names_become_custom_accounts():
    assert account_key("cash") == ("cash", None)
    assert account_key("vendor:123") == ("vendor:123", None)
    assert account_key("expense:rent") == ("expense:rent", None)
    assert account_key("  bank charges ") == ("custom:BANK CHARGES", "BANK CHARGES")


def test_book_adds_up_by_account_and_day():
    from datetime import date

    b = Book()
    b.post("cash", date(2026, 4, 1), debit=100)
    b.post("cash", date(2026, 4, 1), credit=30)
    b.post("cash", date(2026, 4, 2), debit=5)
    b.post("custom:X", date(2026, 4, 2), debit=0, credit=0)  # nothing to post
    assert b.days["cash"][date(2026, 4, 1)] == [100.0, 30.0]
    assert b.days["cash"][date(2026, 4, 2)] == [5.0, 0.0]
    assert "custom:X" not in b.days


def test_balance_side():
    assert _signed(100, 30) == {"amount": 70.0, "side": "Dr", "signed": 70.0}
    assert _signed(30, 100) == {"amount": 70.0, "side": "Cr", "signed": -70.0}
    assert _signed(5, 5)["amount"] == 0
