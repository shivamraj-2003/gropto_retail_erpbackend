"""Vendor registration numbers: format checks and tidying. No database needed."""

import pytest

from app.services.vendor_ids import check_pan_matches_gstin, clean_vendor_ids, gstin_state_code, pan_from_gstin, tidy

GOOD_GSTIN = "29ABCDE1234F1Z5"
GOOD_CIN = "U12345MH2020PTC123456"


def test_numbers_are_saved_in_capitals_without_spaces():
    out = clean_vendor_ids({"gst_number": " 29abcde1234f1z5 ", "pan_number": "abcde 1234 f", "cin_number": GOOD_CIN.lower()})
    assert out == {"gst_number": GOOD_GSTIN, "pan_number": "ABCDE1234F", "cin_number": GOOD_CIN}


def test_empty_values_become_none():
    assert clean_vendor_ids({"gst_number": "  ", "pan_number": "", "pincode": " "}) == {"gst_number": None, "pan_number": None, "pincode": None}
    assert tidy(None) is None


@pytest.mark.parametrize(
    "field,value,word",
    [
        ("gst_number", "29ABCDE1234F1Z", "GSTIN"),  # one short
        ("gst_number", "50ABCDE1234F1Z5", "GSTIN"),  # not a state code
        ("gst_number", "29ABCDE1234F1X5", "GSTIN"),  # 14th character must be Z
        ("pan_number", "ABCD1234FE", "PAN"),
        ("cin_number", "X12345MH2020PTC123456", "CIN"),  # must start with L or U
        ("fssai_number", "1234567", "FSSAI"),
        ("bank_ifsc", "HDFC1001234", "IFSC"),  # fifth character must be 0
        ("pincode", "012345", "Pincode"),
    ],
)
def test_wrong_formats_are_refused_with_a_plain_message(field, value, word):
    with pytest.raises(ValueError, match=word):
        clean_vendor_ids({field: value})


def test_only_the_fields_given_are_checked():
    assert clean_vendor_ids({"pan_number": "ABCDE1234F"}) == {"pan_number": "ABCDE1234F"}


def test_state_and_pan_are_read_out_of_the_gstin():
    assert gstin_state_code(GOOD_GSTIN) == "29"
    assert pan_from_gstin(GOOD_GSTIN) == "ABCDE1234F"
    assert gstin_state_code(None) is None
    assert pan_from_gstin("29ABC") is None


def test_pan_must_match_the_one_inside_the_gstin():
    check_pan_matches_gstin(GOOD_GSTIN, "ABCDE1234F")
    check_pan_matches_gstin(GOOD_GSTIN, None)
    check_pan_matches_gstin(None, "ZZZZZ9999Z")
    with pytest.raises(ValueError, match="does not match"):
        check_pan_matches_gstin(GOOD_GSTIN, "ZZZZZ9999Z")
