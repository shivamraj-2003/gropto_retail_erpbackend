"""The numbers on a vendor's purchase bill.

A purchase line is quantity x cost, with the cost taken as the vendor's rate BEFORE GST; GST at the product's rate is
added on top. Within a state the tax splits into CGST + SGST (half each); between states it is IGST.
"""

from decimal import ROUND_HALF_UP, Decimal


def _r2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def build_bill(lines: list[dict], *, inter_state: bool) -> dict:
    """lines: dicts with quantity, unit_cost, tax_rate. Returns the lines with their tax, a summary per GST rate and totals."""
    out_lines = []
    by_rate: dict[Decimal, dict[str, Decimal]] = {}
    taxable_total = Decimal(0)
    for ln in lines:
        qty, cost, rate = Decimal(str(ln["quantity"])), Decimal(str(ln["unit_cost"])), Decimal(str(ln["tax_rate"]))
        taxable = _r2(qty * cost)
        gst = _r2(taxable * rate / 100)
        if inter_state:
            cgst = sgst = Decimal(0)
            igst = gst
        else:
            cgst = _r2(gst / 2)
            sgst = gst - cgst  # the two halves always add back to the tax
            igst = Decimal(0)
        out_lines.append({**ln, "taxable": float(taxable), "cgst": float(cgst), "sgst": float(sgst), "igst": float(igst), "gst": float(gst), "total": float(taxable + gst)})
        bucket = by_rate.setdefault(rate, {"taxable": Decimal(0), "cgst": Decimal(0), "sgst": Decimal(0), "igst": Decimal(0)})
        bucket["taxable"] += taxable
        bucket["cgst"] += cgst
        bucket["sgst"] += sgst
        bucket["igst"] += igst
        taxable_total += taxable

    gst_total = sum((b["cgst"] + b["sgst"] + b["igst"] for b in by_rate.values()), Decimal(0))
    exact = taxable_total + gst_total
    rounded = exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return {
        "lines": out_lines,
        "by_rate": [
            {"rate": float(rate), **{k: float(v) for k, v in vals.items()}} for rate, vals in sorted(by_rate.items(), key=lambda kv: kv[0])
        ],
        "totals": {
            "taxable": float(taxable_total),
            "cgst": float(sum((b["cgst"] for b in by_rate.values()), Decimal(0))),
            "sgst": float(sum((b["sgst"] for b in by_rate.values()), Decimal(0))),
            "igst": float(sum((b["igst"] for b in by_rate.values()), Decimal(0))),
            "gst": float(gst_total),
            "round_off": float(rounded - exact),
            "invoice_total": float(rounded),
        },
        "inter_state": inter_state,
    }
