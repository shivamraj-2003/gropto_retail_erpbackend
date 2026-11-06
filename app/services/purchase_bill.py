"""The numbers on a vendor's purchase bill.

A purchase line is receipt quantity x rate (the vendor's rate BEFORE GST). From that:

    amount     = receipt qty x rate
    taxable    = amount - discount
    GST        = taxable x GST code %            (the line's own code, else the product's rate)
    surcharge  = taxable x surcharge %
    net amount = taxable + GST + cess + surcharge

Free goods cost nothing and rejected / broken goods never enter stock: stock-in = receipt + free - reject - H.L.
Within a state the GST splits into CGST + SGST (half each); between states it is IGST.
"""

from decimal import ROUND_HALF_UP, Decimal

ALLOWED_GST_RATES = (0, 0.1, 0.25, 1.5, 3, 5, 12, 18, 28, 40)


def _r2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def stock_in(receipt, free=0, reject=0, hl=0) -> Decimal:
    """What goes into stock from one line."""
    return _d(receipt) + _d(free) - _d(reject) - _d(hl)


def line_amounts(*, receipt, rate, discount=0, gst_rate=0, cess=0, surcharge_rate=0) -> dict[str, Decimal]:
    """The money on one line, rounded to paise the way the vendor's bill rounds it."""
    amount = _r2(_d(receipt) * _d(rate))
    taxable = amount - _r2(_d(discount))
    gst = _r2(taxable * _d(gst_rate) / 100)
    surcharge = _r2(taxable * _d(surcharge_rate) / 100)
    cess = _r2(_d(cess))
    return {"amount": amount, "taxable": taxable, "gst": gst, "surcharge": surcharge, "cess": cess, "net": taxable + gst + cess + surcharge}


def build_bill(lines: list[dict], *, inter_state: bool) -> dict:
    """lines: dicts with quantity (receipt), unit_cost, tax_rate and, when the entry carries them, discount, cess,
    surcharge_rate, free_qty, reject_qty, hl_qty. Returns the lines with their tax, a summary per GST rate and totals."""
    out_lines = []
    by_rate: dict[Decimal, dict[str, Decimal]] = {}
    taxable_total = discount_total = cess_total = surcharge_total = Decimal(0)
    for ln in lines:
        rate = _d(ln["tax_rate"])
        amt = line_amounts(
            receipt=ln["quantity"], rate=ln["unit_cost"], discount=ln.get("discount", 0), gst_rate=rate,
            cess=ln.get("cess", 0), surcharge_rate=ln.get("surcharge_rate", 0),
        )
        taxable, gst = amt["taxable"], amt["gst"]
        if inter_state:
            cgst = sgst = Decimal(0)
            igst = gst
        else:
            cgst = _r2(gst / 2)
            sgst = gst - cgst  # the two halves always add back to the tax
            igst = Decimal(0)
        out_lines.append(
            {
                **ln,
                "amount": float(amt["amount"]),
                "taxable": float(taxable),
                "cgst": float(cgst),
                "sgst": float(sgst),
                "igst": float(igst),
                "gst": float(gst),
                "cess": float(amt["cess"]),
                "surcharge": float(amt["surcharge"]),
                "total": float(amt["net"]),
                "stock_in": float(stock_in(ln["quantity"], ln.get("free_qty", 0), ln.get("reject_qty", 0), ln.get("hl_qty", 0))),
            }
        )
        bucket = by_rate.setdefault(rate, {"taxable": Decimal(0), "cgst": Decimal(0), "sgst": Decimal(0), "igst": Decimal(0)})
        bucket["taxable"] += taxable
        bucket["cgst"] += cgst
        bucket["sgst"] += sgst
        bucket["igst"] += igst
        taxable_total += taxable
        discount_total += _r2(_d(ln.get("discount", 0)))
        cess_total += amt["cess"]
        surcharge_total += amt["surcharge"]

    gst_total = sum((b["cgst"] + b["sgst"] + b["igst"] for b in by_rate.values()), Decimal(0))
    exact = taxable_total + gst_total + cess_total + surcharge_total
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
            "discount": float(discount_total),
            "cess": float(cess_total),
            "surcharge": float(surcharge_total),
            "round_off": float(rounded - exact),
            "invoice_total": float(rounded),
        },
        "inter_state": inter_state,
    }
