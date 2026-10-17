"""Checks and tidying for the tax and registration numbers a vendor is saved with.

Format only (a typo is caught; whether the number is really registered is not checked). Everything is saved in
capitals with spaces removed. Each check raises ValueError with a message fit to show on screen.
"""

import re

GSTIN_RE = re.compile(r"^(0[1-9]|[1-2]\d|3[0-8]|9[79])[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")
CIN_RE = re.compile(r"^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
PINCODE_RE = re.compile(r"^[1-9]\d{5}$")
FSSAI_RE = re.compile(r"^\d{14}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def tidy(value: str | None) -> str | None:
    """Capitals, no spaces; empty becomes None."""
    out = (value or "").strip().upper().replace(" ", "")
    return out or None


def gstin_state_code(gstin: str | None) -> str | None:
    """The two-digit GST state code at the front of a GSTIN."""
    g = tidy(gstin)
    return g[:2] if g and len(g) >= 2 and g[:2].isdigit() else None


def pan_from_gstin(gstin: str | None) -> str | None:
    """Characters 3 to 12 of a GSTIN are the holder's PAN."""
    g = tidy(gstin)
    return g[2:12] if g and len(g) == 15 else None


def clean_vendor_ids(values: dict) -> dict:
    """Validate and tidy the ID-type fields present in `values`; returns the cleaned versions of just those keys."""
    out: dict = {}
    rules = {
        "gst_number": (GSTIN_RE, "GSTIN must be 15 characters like 29ABCDE1234F1Z5"),
        "pan_number": (PAN_RE, "PAN must be 10 characters like ABCDE1234F"),
        "cin_number": (CIN_RE, "CIN must be 21 characters like U12345MH2020PTC123456"),
        "fssai_number": (FSSAI_RE, "FSSAI licence must be 14 digits"),
        "bank_ifsc": (IFSC_RE, "IFSC must be 11 characters like HDFC0001234"),
    }
    for key, (pattern, message) in rules.items():
        if key in values:
            v = tidy(values[key])
            if v is not None and not pattern.match(v):
                raise ValueError(message)
            out[key] = v
    if "pincode" in values:
        p = (values["pincode"] or "").strip().replace(" ", "")
        if p and not PINCODE_RE.match(p):
            raise ValueError("Pincode must be 6 digits")
        out["pincode"] = p or None
    return out


def check_pan_matches_gstin(gstin: str | None, pan: str | None) -> None:
    """When both are given, the PAN has to be the one inside the GSTIN."""
    inside = pan_from_gstin(gstin)
    if inside and pan and inside != pan:
        raise ValueError(f"The PAN {pan} does not match the PAN inside the GSTIN ({inside})")
