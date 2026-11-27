from app.api.v1.statements import by_supply


def row(supply, rate, taxable, cgst=0.0, sgst=0.0, igst=0.0):
    return {"supply": supply, "rate": rate, "taxable": taxable, "cgst": cgst, "sgst": sgst, "igst": igst, "tax": round(cgst + sgst + igst, 2)}


def test_local_and_central_are_split_and_the_rates_are_added_back_together():
    rates, supplies = by_supply([row("local", 18.0, 100, 9, 9), row("central", 18.0, 200, igst=36), row("local", 5.0, 50, 1.25, 1.25)])
    assert [(r["rate"], r["taxable"], r["tax"]) for r in rates] == [(5.0, 50, 2.5), (18.0, 300, 54)]
    local, central = supplies
    assert (local["key"], local["taxable"], local["igst"], [r["rate"] for r in local["rates"]]) == ("local", 150, 0, [5.0, 18.0])
    assert (central["key"], central["taxable"], central["igst"], central["cgst"]) == ("central", 200, 36, 0)


def test_both_heads_are_there_when_nothing_was_bought():
    rates, supplies = by_supply([])
    assert rates == []
    assert [(s["key"], s["taxable"], s["rates"]) for s in supplies] == [("local", 0, []), ("central", 0, [])]
