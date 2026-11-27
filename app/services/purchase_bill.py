"""The numbers on a vendor's purchase bill.

A purchase line is receipt quantity x rate. A vendor's bill quotes the rate either BEFORE GST (exclusive, the usual) or with
GST already in it (inclusive). Either way the discount comes off the value before GST, never off the tax, and the tax is
charged on what is left:

    amount     = receipt qty x rate
    base       = amount                          (exclusive)    or    amount / (1 + GST %)    (inclusive)
    taxable    = base - discount
    GST        = taxable x GST code %            (the line's own code, else the product's rate)
    surcharge  = taxable x surcharge %
    net amount = taxable + GST + cess + surcharge

With an inclusive rate and no discount, taxable + GST is exactly the amount (no paisa is lost to rounding).

Free goods cost nothing and rejected / broken goods never enter stock: stock-in = receipt + free - reject - H.L.
Within a state the GST splits into CGST + SGST (half each); between states it is IGST.
"""

from decimal import ROUND_HALF_UP, Decimal

ALLOWED_GST_RATES = (0, 0.1, 0.25, 1.5, 3, 5, 12, 18, 28, 40)

# SQL for a saved line's value before GST (purchase_items as `pi`). The net amount less the taxes is right whether the rate was
# quoted with GST or without; lines saved before net amounts existed are rate x quantity less discount.
LINE_VALUE = (
    "coalesce(pi.net_amount - coalesce(pi.gst_amount,0) - coalesce(pi.cess_amount,0) - coalesce(pi.surcharge_amount,0), "
    "pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0))"
)


def _r2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def stock_in(receipt, free=0, reject=0, hl=0) -> Decimal:
    """What goes into stock from one line."""
    return _d(receipt) + _d(free) - _d(reject) - _d(hl)


def line_amounts(*, receipt, rate, discount=0, gst_rate=0, cess=0, surcharge_rate=0, inclusive=False) -> dict[str, Decimal]:
    """The money on one line, rounded to paise the way the vendor's bill rounds it. `inclusive`: the rate already has GST in it."""
    amount = _r2(_d(receipt) * _d(rate))
    off = _r2(_d(discount))
    if inclusive:
        factor = 1 + _d(gst_rate) / 100
        taxable = _r2(amount / factor) - off
        gst = _r2(amount - off * factor) - taxable if taxable else Decimal(0)
    else:
        taxable = amount - off
        gst = _r2(taxable * _d(gst_rate) / 100)
    surcharge = _r2(taxable * _d(surcharge_rate) / 100)
    cess = _r2(_d(cess))
    return {"amount": amount, "taxable": taxable, "gst": gst, "surcharge": surcharge, "cess": cess, "net": taxable + gst + cess + surcharge}


def build_bill(lines: list[dict], *, inter_state: bool, inclusive: bool = False) -> dict:
    """lines: dicts with quantity (receipt), unit_cost, tax_rate and, when the entry carries them, discount, cess,
    surcharge_rate, free_qty, reject_qty, hl_qty. Returns the lines with their tax, a summary per GST rate and totals."""
    out_lines = []
    by_rate: dict[Decimal, dict[str, Decimal]] = {}
    taxable_total = discount_total = cess_total = surcharge_total = Decimal(0)
    for ln in lines:
        rate = _d(ln["tax_rate"])
        amt = line_amounts(
            receipt=ln["quantity"], rate=ln["unit_cost"], discount=ln.get("discount", 0), gst_rate=rate,
            cess=ln.get("cess", 0), surcharge_rate=ln.get("surcharge_rate", 0), inclusive=inclusive,
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
