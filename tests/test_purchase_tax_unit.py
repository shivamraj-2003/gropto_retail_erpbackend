"""Purchase lines sorted for GSTR-3B, and the heads the new ledger accounts fall under. No database needed."""

from app.api.v1.statements import head_of
from app.services.gst_returns import purchase_tax


def bought(**over):
    return {"purchase_id": "b1", "value": 90, "taxable": 100, "gst": 18, "rate": 18, "inter_state": False, "without_gst": False,
            "reverse_charge": False, "input_credit": True, "vendor_gstin": "09ABCDE1234F1Z5", **over}


def test_an_ordinary_bill_gives_credit_in_two_halves():
    t = purchase_tax([bought()])
    assert t["itc"] == {"igst": 0, "cgst": 9, "sgst": 9} and t["reverse"]["txval"] == 0 and t["bills_with_credit"] == {"b1"}


def test_reverse_charge_is_owed_and_claimable_even_from_an_unregistered_vendor():
    t = purchase_tax([bought(reverse_charge=True, vendor_gstin=None, inter_state=True)])
    assert t["reverse"] == {"txval": 100, "igst": 18, "cgst": 0, "sgst": 0}
    assert t["itc"]["igst"] == 18 and sum(t["not_claimed"].values()) == 0


def test_credit_marked_not_eligible_is_not_taken():
    t = purchase_tax([bought(input_credit=False), bought(input_credit=False, reverse_charge=True)])
    assert sum(t["itc"].values()) == 0 and sum(t["ineligible"].values()) == 36
    assert t["reverse"]["cgst"] + t["reverse"]["sgst"] == 18  # still owed to the government


def test_the_new_accounts_have_their_heads():
    assert head_of("gst_rcm", "GST payable on reverse charge") == "current_liabilities"
    assert head_of("expense:GST not claimable", "GST not claimable") == "indirect"
    assert head_of("custom:DISCOUNT RECEIVED", "DISCOUNT RECEIVED") == "income"
