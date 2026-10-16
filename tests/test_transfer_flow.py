"""Stock effects of the transfer scenarios, with the stock functions replaced by recorders (no database).

Each test reads like the shop floor: "sent 5, received 4" and then what the ledger shows.
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.models import Product
from app.models.models_phase2 import Transfer, TransferItem
from app.schemas.schemas_phase2 import TransferCreate, TransferItemIn, TransferReceive, TransferReceiveItem
from app.services import transfers as svc

SRC, DEST, PRODUCT = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class FakeDB:
    def __init__(self):
        self.added = []

    async def get(self, model, key):
        if model is Product:
            return SimpleNamespace(id=key, name="Tata Salt 1kg", sku="SALT")
        return None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def refresh(self, obj, attribute_names=None):
        if isinstance(obj, Transfer):
            obj.items = [o for o in self.added if isinstance(o, TransferItem) and o.transfer_id == obj.id]

    async def delete(self, obj):
        self.added.remove(obj) if obj in self.added else None


class Ledger(list):
    """The recorded stock effects, plus a dial for how much the sender has in stock."""

    stock: dict


@pytest.fixture
def ledger(monkeypatch):
    """Records every stock effect: ('move', place, delta, reason) / ('transit', delta) / ('damaged', delta) / ('wh', place, delta)."""
    rows = Ledger()
    stock = {"source": 100.0}

    async def apply_movement(db, *, product_id, store_id, delta, reason_code, **_):
        rows.append(("move", "source" if store_id == SRC else "dest", delta, reason_code))

    async def adjust_in_transit(db, *, product_id, store_id, delta):
        rows.append(("transit", delta))

    async def adjust_damaged(db, *, product_id, store_id, delta):
        rows.append(("damaged", delta))

    async def adjust_warehouse_balance(db, *, product_id, warehouse_id, delta):
        rows.append(("wh", "source" if warehouse_id == SRC else "dest", delta))

    async def get_balance(db, *, product_id, store_id):
        return stock["source"]

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "apply_movement", apply_movement)
    monkeypatch.setattr(svc, "adjust_in_transit", adjust_in_transit)
    monkeypatch.setattr(svc, "adjust_damaged", adjust_damaged)
    monkeypatch.setattr(svc, "adjust_warehouse_balance", adjust_warehouse_balance)
    monkeypatch.setattr(svc, "get_balance", get_balance)
    monkeypatch.setattr(svc, "_assert_place_exists", noop)
    monkeypatch.setattr(svc, "write_audit", noop)
    rows.stock = stock
    return rows


def user(super_admin=True):
    return SimpleNamespace(user_id=uuid.uuid4(), role_code="admin", device_id=None, is_super_admin=super_admin)


def run(coro):
    return asyncio.run(coro)


def create(db, qty=5, draft=False):
    payload = TransferCreate(
        source_type="store", source_id=SRC, dest_type="store", dest_id=DEST, items=[TransferItemIn(product_id=PRODUCT, dispatched_qty=qty)], draft=draft
    )
    return run(svc.dispatch_transfer(db, current=user(), payload=payload))


def receive(db, t, good, damaged=0, close=False):
    items = [TransferReceiveItem(transfer_item_id=t.items[0].id, received_qty=good, damaged_qty=damaged)]
    return run(svc.receive_transfer(db, current=user(), transfer=t, payload=TransferReceive(items=items, close=close)))


def resolve(db, t, action=None, super_admin=True):
    actions = {str(t.items[0].id): action} if action else {}
    return run(svc.resolve_discrepancy(db, current=user(super_admin), transfer=t, note="checked", actions=actions))


def test_exact_receipt(ledger):
    db = FakeDB()
    t = create(db, 5)
    assert t.status == "dispatched"
    assert ledger == [("move", "source", -5, "transfer_out"), ("transit", 5)]
    receive(db, t, 5)
    assert t.status == "received"
    assert ("move", "dest", 5, "transfer_in") in ledger
    assert ledger.count(("transit", -5)) == 1


def test_sent_five_received_four_close_then_write_off(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 4, close=True)
    assert t.status == "discrepancy"
    # 4 went into the destination; the pipeline is cleared (4 arrived + the 1 that never will)
    assert ("move", "dest", 4, "transfer_in") in ledger
    assert sum(r[1] for r in ledger if r[0] == "transit") == 0
    before = len(ledger)
    resolve(db, t, "write_off")
    assert t.status == "resolved"
    assert len(ledger) == before, "writing it off moves no stock; the loss stays on the transfer"
    assert (float(t.items[0].settled_qty), t.items[0].resolution) == (1, "write_off")


def test_missing_unit_that_never_left_goes_back_to_the_sender(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 4, close=True)
    resolve(db, t, "return_to_source")
    assert ("move", "source", 1, "transfer_return") in ledger
    assert t.status == "resolved"


def test_partial_delivery_then_the_rest(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 3)
    assert t.status == "partially_received" and t.received_at is None
    assert [r for r in ledger if r[0] == "move" and r[3] == "transfer_in"] == [("move", "dest", 3, "transfer_in")]
    receive(db, t, 2)
    assert t.status == "received"
    assert float(t.items[0].received_qty) == 5
    assert sum(r[2] for r in ledger if r[0] == "move" and r[3] == "transfer_in") == 5
    assert sum(r[1] for r in ledger if r[0] == "transit") == 0, "the inbound figure ends at zero"


def test_partial_then_nothing_more_is_coming(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 3)
    run(svc.receive_transfer(db, current=user(), transfer=t, payload=TransferReceive(items=[], close=True)))
    assert t.status == "discrepancy"
    assert sum(r[1] for r in ledger if r[0] == "transit") == 0


def test_damaged_units_are_not_added_to_sellable_stock(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 4, damaged=1)
    assert t.status == "discrepancy"
    assert ("move", "dest", 4, "transfer_in") in ledger
    assert ("damaged", 1) in ledger
    resolve(db, t)  # nothing missing or extra: no choice needed
    assert t.status == "resolved" and t.items[0].resolution == "damaged"


def test_more_arrived_than_sent_can_correct_the_sender(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 6)
    assert t.status == "discrepancy"
    resolve(db, t, "reduce_sender")
    assert ("move", "source", -1, "transfer_count_correction") in ledger


def test_more_arrived_than_sent_can_go_back(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 6)
    resolve(db, t, "send_back")
    assert ("move", "dest", -1, "transfer_send_back") in ledger


def test_a_shortage_cannot_be_closed_without_a_decision(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 4, close=True)
    with pytest.raises(HTTPException) as e:
        resolve(db, t)
    assert e.value.status_code == 400 and "Tata Salt" in e.value.detail
    assert t.status == "discrepancy"


def test_receiving_nothing_is_refused(ledger):
    db = FakeDB()
    t = create(db, 5)
    with pytest.raises(HTTPException):
        run(svc.receive_transfer(db, current=user(), transfer=t, payload=TransferReceive(items=[], close=False)))


def test_a_closed_transfer_cannot_be_received_again(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 5)
    with pytest.raises(HTTPException) as e:
        receive(db, t, 1)
    assert e.value.status_code == 409


def test_cancel_only_before_anything_arrived(ledger):
    db = FakeDB()
    t = create(db, 5)
    receive(db, t, 2)
    with pytest.raises(HTTPException):
        run(svc.cancel_transfer(db, current=user(), transfer=t, reason="x"))
    t2 = create(db, 5)
    run(svc.cancel_transfer(db, current=user(), transfer=t2, reason="changed my mind"))
    assert t2.status == "cancelled"
    assert ("move", "source", 5, "transfer_cancelled") in ledger


def test_draft_moves_no_stock_until_it_is_sent(ledger):
    db = FakeDB()
    t = create(db, 5, draft=True)
    assert t.status == "draft"
    assert ledger == [], "saving a draft touches no stock"
    run(svc.send_draft(db, current=user(), transfer=t))
    assert t.status == "dispatched"
    assert ledger == [("move", "source", -5, "transfer_out"), ("transit", 5)]


def test_draft_can_be_changed_and_deleted_but_not_after_sending(ledger):
    db = FakeDB()
    t = create(db, 5, draft=True)
    payload = TransferCreate(source_type="store", source_id=SRC, dest_type="store", dest_id=DEST, items=[TransferItemIn(product_id=PRODUCT, dispatched_qty=8)])
    run(svc.update_draft(db, current=user(), transfer=t, payload=payload))
    assert [float(i.dispatched_qty) for i in t.items] == [8]
    run(svc.send_draft(db, current=user(), transfer=t))
    with pytest.raises(HTTPException):
        run(svc.update_draft(db, current=user(), transfer=t, payload=payload))
    with pytest.raises(HTTPException):
        run(svc.delete_draft(db, current=user(), transfer=t))
    d = create(db, 2, draft=True)
    run(svc.delete_draft(db, current=user(), transfer=d))


def test_a_draft_cannot_be_sent_when_the_stock_is_gone(ledger):
    db = FakeDB()
    t = create(db, 5, draft=True)
    ledger.stock["source"] = 2
    with pytest.raises(HTTPException) as e:
        run(svc.send_draft(db, current=user(), transfer=t))
    assert e.value.status_code == 409 and t.status == "draft"


def test_a_warehouse_destination_gets_good_units_only(ledger):
    db = FakeDB()
    payload = TransferCreate(
        source_type="store", source_id=SRC, dest_type="warehouse", dest_id=DEST, items=[TransferItemIn(product_id=PRODUCT, dispatched_qty=5)]
    )
    t = run(svc.dispatch_transfer(db, current=user(), payload=payload))
    receive(db, t, 4, damaged=1)
    assert ("wh", "dest", 4) in ledger
    assert not any(r[0] == "damaged" for r in ledger), "a warehouse has no damaged bucket; the loss stays on the transfer"
