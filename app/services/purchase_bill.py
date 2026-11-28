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

The bill's "Taxes & Levies" options change the rule:

    GST before discount       GST is charged on the whole base; the discount comes off afterwards and does not lower the tax.
    Cash discount after GST   the same money, but the discount is a cash discount: the goods are booked at the whole base and the
                              discount is income ("Discount received"). A discount % is then a % of the amount with GST.
    Reverse charge (RCM)      the buyer pays the GST and cess to the government, so the vendor is owed the net without them.

Free goods cost nothing and rejected / broken goods never enter stock: stock-in = receipt + free - reject - H.L.
Within a state the GST splits into CGST + SGST (half each); between states it is IGST.
"""

from decimal import ROUND_HALF_UP, Decimal

ALLOWED_GST_RATES = (0, 0.1, 0.25, 1.5, 3, 5, 12, 18, 28, 40)

# SQL for saved lines (purchase_items as `pi`, purchases as `pu`).
# What a line comes to with its taxes, after every discount. Lines saved before net amounts existed: rate x quantity less discount.
LINE_NET = "coalesce(pi.net_amount, pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0))"
# The value of the goods before GST: what the purchase is booked at. A cash discount after GST does not lower it.
LINE_VALUE = (
    "coalesce(pi.net_amount - coalesce(pi.gst_amount,0) - coalesce(pi.cess_amount,0) - coalesce(pi.surcharge_amount,0) "
    "+ coalesce(pi.cash_discount,0), pi.unit_cost * pi.quantity - coalesce(pi.discount_amount,0))"
)
# The value GST was charged on (more than the goods' value when GST was worked before the discount).
LINE_TAXABLE = f"coalesce(pi.taxable_amount, {LINE_VALUE})"
# What the ledger's Purchases account takes: the goods' value with cess and surcharge.
PURCHASE_ACCOUNT = f"({LINE_NET} - coalesce(pi.gst_amount,0) + coalesce(pi.cash_discount,0))"
# Tax the buyer pays to the government instead of the vendor, and what is left for the vendor.
REVERSE_TAX = "(case when pu.reverse_charge then coalesce(pi.gst_amount,0) + coalesce(pi.cess_amount,0) else 0 end)"
VENDOR_AMOUNT = f"({LINE_NET} - {REVERSE_TAX})"


def inter_state_sql(vendor: str = "v", store: str = "st") -> str:
    """Between states or not: as ticked on the bill; bills saved without that go by the two GST numbers."""
    return (
        f"coalesce(pu.inter_state, (left(coalesce({vendor}.gst_number,''),2) <> '' and left(coalesce({store}.gstin,''),2) <> '' "
        f"and left({vendor}.gst_number,2) <> left({store}.gstin,2)))"
    )


def _r2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def stock_in(receipt, free=0, reject=0, hl=0) -> Decimal:
    """What goes into stock from one line."""
    return _d(receipt) + _d(free) - _d(reject) - _d(hl)


def line_amounts(
    *, receipt, rate, discount=0, gst_rate=0, cess=0, surcharge_rate=0, inclusive=False,
    gst_before_discount=False, cash_discount_after_gst=False, reverse_charge=False,
) -> dict:
    """The money on one line, rounded to paise the way the vendor's bill rounds it (the options are explained above).

    taxable = what GST is charged on; value = what the goods are booked at; net = goods with taxes after discounts;
    payable = what the vendor is owed (net, less the taxes under reverse charge)."""
    amount = _r2(_d(receipt) * _d(rate))
    off = _r2(_d(discount))
    factor = 1 + _d(gst_rate) / 100
    base = _r2(amount / factor) if inclusive else amount
    on_full = gst_before_discount or cash_discount_after_gst  # the discount does not lower the tax
    taxable = base if on_full else base - off
    if not inclusive:
        gst = _r2(taxable * _d(gst_rate) / 100)
    elif on_full:
        gst = amount - base
    else:
        gst = _r2(amount - off * factor) - taxable if taxable else Decimal(0)
    surcharge = _r2(taxable * _d(surcharge_rate) / 100)
    cess = _r2(_d(cess))
    net = base - off + gst + cess + surcharge
    reverse_tax = gst + cess if reverse_charge else Decimal(0)
    return {
        "amount": amount, "base": base, "taxable": taxable, "value": base if cash_discount_after_gst else base - off, "gst": gst,
        "surcharge": surcharge, "cess": cess, "cash_discount": off if cash_discount_after_gst else Decimal(0), "net": net,
        "reverse_tax": reverse_tax, "payable": net - reverse_tax,
        # a cash discount may eat into the tax too; any other discount cannot be more than the goods are worth
        "over_discount": off > (base + gst if cash_discount_after_gst else base),
    }


def build_bill(
    lines: list[dict], *, inter_state: bool, inclusive: bool = False, gst_before_discount: bool = False,
    cash_discount_after_gst: bool = False, reverse_charge: bool = False,
) -> dict:
    """lines: dicts with quantity (receipt), unit_cost, tax_rate and, when the entry carries them, discount, cess,
    surcharge_rate, free_qty, reject_qty, hl_qty. Returns the lines with their tax, a summary per GST rate and totals."""
    out_lines = []
    by_rate: dict[Decimal, dict[str, Decimal]] = {}
    taxable_total = discount_total = cess_total = surcharge_total = Decimal(0)
    cash_total = reverse_total = payable_total = Decimal(0)
    for ln in lines:
        rate = _d(ln["tax_rate"])
        amt = line_amounts(
            receipt=ln["quantity"], rate=ln["unit_cost"], discount=ln.get("discount", 0), gst_rate=rate,
            cess=ln.get("cess", 0), surcharge_rate=ln.get("surcharge_rate", 0), inclusive=inclusive, gst_before_discount=gst_before_discount,
            cash_discount_after_gst=cash_discount_after_gst, reverse_charge=reverse_charge,
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
                "total": float(amt["payable"]),
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
        cash_total += amt["cash_discount"]
        reverse_total += amt["reverse_tax"]
        payable_total += amt["payable"]

    gst_total = sum((b["cgst"] + b["sgst"] + b["igst"] for b in by_rate.values()), Decimal(0))
    exact = payable_total  # what the vendor is owed, before rounding to the rupee
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
            "cash_discount": float(cash_total),
            "reverse_charge_tax": float(reverse_total),
            "round_off": float(rounded - exact),
            "invoice_total": float(rounded),
        },
        "inter_state": inter_state,
    }
