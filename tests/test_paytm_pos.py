"""Paytm POS: signing, request building, reading Paytm's answers and the safety rules. No database, no network.

Paytm answers here are mocked from the field names on Paytm's Sale / Status Enquiry / Void pages. They prove our
reading of those answers, not that a real machine works: that needs the physical terminal."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import paytm_flow as flow
from app.services import paytm_pos as paytm

KEY = "0123456789abcdef"  # 16 characters, like a Paytm merchant key


def _p(**kw):
    base = dict(
        id=uuid.uuid4(), store_id=uuid.uuid4(), status=paytm.SENT, status_message="", amount=Decimal("2500.00"),
        sale_id=None, sent_at=None, last_enquiry_at=None, created_at=None, is_active=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _answer(status="", code="", msg="", **body):
    return {"head": {}, "body": {"resultInfo": {"resultStatus": status, "resultCodeId": code, "resultMsg": msg}, **body}}


# ---- signing

def test_a_signature_verifies_with_the_same_key_and_body():
    body = '{"paytmMid":"M","paytmTid":"26537016"}'
    sig = paytm.generate_signature(body, KEY)
    assert paytm.verify_signature(body, KEY, sig)


def test_a_changed_body_or_key_fails_verification():
    sig = paytm.generate_signature('{"a":1}', KEY)
    assert not paytm.verify_signature('{"a":2}', KEY, sig)
    assert not paytm.verify_signature('{"a":1}', "fedcba9876543210", sig)
    assert not paytm.verify_signature('{"a":1}', KEY, "not-a-signature")


def test_the_signature_hides_a_hash_of_body_pipe_salt():
    # Paytm's utility: AES(sha256(body|salt) + salt). Open it with the key and check the pieces.
    sig = paytm.generate_signature("hello", KEY, salt="abcd")
    plain = paytm._decrypt(sig, KEY)
    assert plain == hashlib.sha256(b"hello|abcd").hexdigest() + "abcd"


def test_a_bad_key_length_is_a_clear_error():
    with pytest.raises(paytm.PaytmError):
        paytm.generate_signature("x", "short")


# ---- the request we send

def test_the_amount_goes_to_paytm_in_paise_as_text():
    assert paytm.to_paise(Decimal("2500.00")) == "250000"
    assert paytm.to_paise(227.68) == "22768"
    assert paytm.to_paise("0.10") == "10"


def test_the_sale_body_carries_the_machine_and_the_exact_amount():
    body = paytm.sale_body(mid="MID1", tid="26537016", merchant_txn_id="GPPTMabc", amount_rupees=Decimal("2500"), reference="bill-1", when="2026-10-24 10:00:00")
    assert body["paytmMid"] == "MID1" and body["paytmTid"] == "26537016"
    assert body["transactionAmount"] == "250000"
    assert body["merchantTransactionId"] == "GPPTMabc"
    assert "autoAccept" not in json.dumps(body)  # the cashier or customer accepts on the machine; nothing is faked


def test_the_signed_text_is_the_text_that_is_sent():
    body = {"paytmMid": "M", "paytmTid": "T", "transactionAmount": "100"}
    text = paytm.build_request(body, key=KEY, channel_id="CH1", now="2026-10-24 10:00:00")
    sent = json.loads(text)
    body_text = text[text.index('"body":') + 7 : -1]
    assert json.loads(body_text) == body
    assert paytm.verify_signature(body_text, KEY, sent["head"]["checksum"])
    assert sent["head"]["channelId"] == "CH1"


def test_each_request_gets_a_fresh_alphanumeric_id_in_paytms_length_range():
    a, b = paytm.new_merchant_transaction_id(), paytm.new_merchant_transaction_id()
    assert a != b and a.isalnum() and 8 <= len(a) <= 32 and a.startswith("GPPTM")


def test_a_void_lookup_asks_for_the_void_event():
    body = paytm.lookup_body(mid="M", tid="T", merchant_txn_id="X", when="2026-10-24 10:00:00", void=True)
    assert body["event"] == "VOID"
    assert "event" not in paytm.lookup_body(mid="M", tid="T", merchant_txn_id="X", when="2026-10-24 10:00:00")


# ---- reading Paytm's answers

def test_an_accepted_sale_is_only_sent_not_paid():
    state, _ = paytm.read_sale_answer(_answer("A", "0009", "ACCEPTED_SUCCESS"))
    assert state == paytm.SENT


@pytest.mark.parametrize("code", ["0330", "0007", "0333", "0002", "0233", "9001", "0902"])
def test_a_refused_sale_fails_with_a_readable_reason(code):
    state, message = paytm.read_sale_answer(_answer("F", code, "x"))
    assert state == paytm.FAILED and message


def test_paytms_own_error_leaves_the_result_unknown_not_failed():
    assert paytm.read_sale_answer(_answer("F", "0012", "SYSTEM_ERROR"))[0] == paytm.UNKNOWN


def test_only_success_in_the_status_answer_means_paid():
    assert paytm.read_status_answer(_answer("S", "0000", "Success"))[0] == paytm.SUCCESS
    for pending in [("A", "0009"), ("U", "0010"), ("U", "0030")]:
        assert paytm.read_status_answer(_answer(*pending))[0] == paytm.SENT
    assert paytm.read_status_answer(_answer("F", "0011", "The sale txn has failed"))[0] == paytm.FAILED
    # a checksum or server error says nothing about the payment: never "not paid"
    assert paytm.read_status_answer(_answer("F", "0330", "Invalid checksum"))[0] == paytm.UNKNOWN
    assert paytm.read_status_answer(_answer("F", "0012", "Internal Server Error. Please retry"))[0] == paytm.UNKNOWN
    assert paytm.read_status_answer({})[0] == paytm.UNKNOWN


def test_a_void_status_is_read_on_its_own_terms():
    assert paytm.read_status_answer(_answer("S", "0000", "SUCCESS"), void=True)[0] == paytm.CANCELLED
    assert paytm.read_status_answer(_answer("F", "0180", "VOID transaction not found"), void=True)[0] == paytm.FAILED
    assert paytm.read_status_answer(_answer("U", "0010"), void=True)[0] == paytm.CANCEL_REQUESTED


def test_the_paid_amount_must_match_the_amount_asked_for():
    assert paytm.amount_matches(_answer("S", "0000", transactionAmount="250000"), Decimal("2500"))
    assert not paytm.amount_matches(_answer("S", "0000", transactionAmount="100000"), Decimal("2500"))
    assert not paytm.amount_matches(_answer("S", "0000"), Decimal("2500"))


def test_paid_details_keep_bank_references_only():
    d = paytm.paid_details(_answer("S", "0000", acquirementId="A1", retrievalReferenceNo="R1", authCode="X", cardType="", paytmMid="M"))
    assert d == {"acquirementId": "A1", "retrievalReferenceNo": "R1", "authCode": "X"}


# ---- rules

def test_amounts_must_be_positive_and_within_the_limit():
    assert flow.check_amount(Decimal("2500")) == Decimal("2500.00")
    with pytest.raises(flow.FlowError):
        flow.check_amount(Decimal("0"))
    with pytest.raises(flow.FlowError):
        flow.check_amount(Decimal("500000.01"))


def test_the_store_machine_is_chosen_by_store_and_never_by_guess():
    t1, t2 = _p(), _p()
    assert flow.choose_terminal([t1], None) is t1
    with pytest.raises(flow.FlowError):  # none set up
        flow.choose_terminal([], None)
    with pytest.raises(flow.FlowError):  # two machines: the till must say which
        flow.choose_terminal([t1, t2], None)
    assert flow.choose_terminal([t1, t2], t2.id) is t2
    with pytest.raises(flow.FlowError):  # a machine from another store is not in this store's list
        flow.choose_terminal([t1], uuid.uuid4())
    with pytest.raises(flow.FlowError):
        flow.choose_terminal([_p(is_active=False)], None)


def test_a_waiting_or_unknown_request_blocks_a_new_one_on_the_same_machine():
    assert flow.blocking_payment([_p(status=paytm.SENT)]) is not None
    assert flow.blocking_payment([_p(status=paytm.UNKNOWN)]) is not None
    assert flow.blocking_payment([_p(status=paytm.SUCCESS), _p(status=paytm.FAILED), _p(status=paytm.CANCELLED)]) is None


def test_paytm_is_asked_at_most_every_ten_seconds_and_not_before_the_first_ten():
    now = datetime(2026, 10, 24, 10, 0, 30, tzinfo=timezone.utc)
    fresh = _p(sent_at=now - timedelta(seconds=4))
    assert not flow.due_for_enquiry(fresh, now)
    ready = _p(sent_at=now - timedelta(seconds=12))
    assert flow.due_for_enquiry(ready, now)
    just_asked = _p(sent_at=now - timedelta(seconds=60), last_enquiry_at=now - timedelta(seconds=3))
    assert not flow.due_for_enquiry(just_asked, now)
    assert not flow.due_for_enquiry(_p(status=paytm.SUCCESS), now)


def test_paid_with_the_wrong_amount_is_never_success():
    now = datetime.now(timezone.utc)
    state, _ = flow.next_state(_p(), paytm.SUCCESS, "Paid", code="0000", amount_ok=False, now=now)
    assert state == paytm.UNKNOWN
    state, _ = flow.next_state(_p(), paytm.SUCCESS, "Paid", code="0000", amount_ok=True, now=now)
    assert state == paytm.SUCCESS


def test_no_record_at_paytm_fails_only_after_it_had_time_and_was_never_accepted():
    now = datetime.now(timezone.utc)
    young = _p(status=paytm.UNKNOWN, created_at=now - timedelta(seconds=10), sent_at=None)
    assert flow.next_state(young, paytm.UNKNOWN, "x", code="0404", amount_ok=True, now=now)[0] == paytm.UNKNOWN
    old = _p(status=paytm.UNKNOWN, created_at=now - timedelta(seconds=120), sent_at=None)
    assert flow.next_state(old, paytm.UNKNOWN, "x", code="0404", amount_ok=True, now=now)[0] == paytm.FAILED
    accepted = _p(status=paytm.SENT, created_at=now - timedelta(seconds=120), sent_at=now - timedelta(seconds=100))
    assert flow.next_state(accepted, paytm.UNKNOWN, "x", code="0404", amount_ok=True, now=now)[0] == paytm.UNKNOWN


def test_a_finished_payment_never_changes_state_again():
    now = datetime.now(timezone.utc)
    done = _p(status=paytm.SUCCESS, status_message="Paid")
    assert flow.next_state(done, paytm.FAILED, "x", code="0011", amount_ok=True, now=now)[0] == paytm.SUCCESS


def test_a_failed_cancel_returns_the_request_to_waiting():
    now = datetime.now(timezone.utc)
    assert flow.next_state(_p(status=paytm.CANCEL_REQUESTED), paytm.FAILED, "no", code="0180", amount_ok=True, now=now)[0] == paytm.SENT


def test_a_bill_settles_only_with_a_confirmed_payment_of_its_own_store_and_amount():
    store, sale = uuid.uuid4(), uuid.uuid4()
    paid = _p(status=paytm.SUCCESS, store_id=store, amount=Decimal("2500.00"))
    assert flow.settle_error(paid, store_id=store, amount=2500.0, sale_id=sale) is None
    assert flow.settle_error(_p(status=paytm.SENT, store_id=store), store_id=store, amount=2500.0, sale_id=sale)
    assert flow.settle_error(paid, store_id=uuid.uuid4(), amount=2500.0, sale_id=sale)  # another store
    assert flow.settle_error(paid, store_id=store, amount=2400.0, sale_id=sale)  # different amount
    used = _p(status=paytm.SUCCESS, store_id=store, amount=Decimal("2500.00"), sale_id=uuid.uuid4())
    assert flow.settle_error(used, store_id=store, amount=2500.0, sale_id=sale)  # already used by another bill
    assert flow.settle_error(used, store_id=store, amount=2500.0, sale_id=used.sale_id) is None  # the same bill replayed is fine
