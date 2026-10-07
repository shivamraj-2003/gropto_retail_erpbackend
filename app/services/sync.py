"""Sync push: applies a batch of offline-created sales idempotently, returning one
verdict per item so a single bad row never blocks the rows behind it. Safe under
replay because of three layers of dedup: unique client_idempotency_key on sales,
device-generated primary keys, and unique bill numbers per store+device.
"""


from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Customer, Device, Payment, Role, Sale, SaleItem, Store, SyncFailure, User
from app.models.models_phase2 import GiftVoucher
from app.schemas.schemas import SaleIn, SyncItemVerdict
from app.models.models_phase4 import CouponRedemption
from app.services.audit import write_audit
from app.services.coupons import check_coupon
from app.services.gst import determine_place_of_supply, split_tax
from app.services.inventory import DuplicateMovement, apply_movement, get_balance as get_stock_balance
from app.services.promotion_engine import build_lines, evaluate_and_log, evaluate_preview
from app.services.loyalty import DuplicateLedgerEntry, apply_ledger_entry, get_balance, get_config, get_earn_multiplier
from app.services.wallet import DuplicateWalletEntry, InsufficientWalletBalance, debit_wallet


async def _get_or_create_customer(db: AsyncSession, phone: str | None) -> Customer | None:
    if not phone:
        return None
    result = await db.execute(select(Customer).where(Customer.phone == phone))
    customer = result.scalar_one_or_none()
    if customer is None:
        customer = Customer(phone=phone)
        db.add(customer)
        await db.flush()
    return customer


async def _find_customer(db: AsyncSession, phone: str | None) -> Customer | None:
    if not phone:
        return None
    return (await db.execute(select(Customer).where(Customer.phone == phone))).scalar_one_or_none()


async def process_sale(db: AsyncSession, sale_in: SaleIn) -> SyncItemVerdict:
    existing = await db.get(Sale, sale_in.id)
    if existing is not None:
        return SyncItemVerdict(client_id=sale_in.id, verdict="duplicate", server_id=existing.id)

    existing_by_key = (
        await db.execute(select(Sale).where(Sale.client_idempotency_key == sale_in.client_idempotency_key))
    ).scalar_one_or_none()
    if existing_by_key is not None:
        return SyncItemVerdict(client_id=sale_in.id, verdict="duplicate", server_id=existing_by_key.id)

    savepoint = await db.begin_nested()
    try:
        cashier = await db.get(User, sale_in.cashier_id)
        if cashier is None:
            raise ValueError("Unknown cashier")
        role = await db.get(Role, cashier.role_id)
        device = await db.get(Device, sale_in.device_id)
        if device is None or device.status != "active":
            raise ValueError("Device not active")

        # Point 10 audit fix: place of supply / inter-state determination —
        # defaults to the selling store's own state (the normal walk-in
        # case); only flips to inter-state when a B2B customer_gstin is
        # supplied with a genuinely different state prefix.
        store = await db.get(Store, sale_in.store_id)
        place_of_supply, inter_state = determine_place_of_supply(
            store_state=store.state if store else None, customer_gstin=sale_in.customer_gstin
        )

        # GST-inclusive pricing (per Indian law, MRP already includes GST — tax is
        # backed OUT of unit_price here, never added on top). subtotal ends up
        # meaning "taxable value" (pre-GST), not the old exclusive-pricing gross.
        subtotal = 0.0
        tax_total = 0.0
        line_tax_breakdown: dict[int, tuple[float, float, float, float]] = {}
        for idx, item in enumerate(sale_in.items):
            line_net = item.quantity * item.unit_price - item.line_discount  # GST-inclusive
            rate = float(item.tax_rate_snapshot)
            line_taxable, cgst, sgst, igst = split_tax(line_net, rate, inter_state=inter_state)
            line_tax = cgst + sgst + igst
            subtotal += line_taxable
            tax_total += line_tax
            line_tax_breakdown[idx] = (line_taxable, cgst, sgst, igst)

        line_discounts = sum(item.line_discount for item in sale_in.items)
        discount_total = sale_in.discount_total + line_discounts
        # Only the bill-level discount reduces the final total here — line
        # discounts are already netted into subtotal/tax_total above.
        grand_total = subtotal + tax_total - sale_in.discount_total

        # Offers and coupons are business-configured, not a cashier's call, so
        # they don't count against the role discount limit — but only the share
        # the server can reproduce from its own rules does. Anything the till
        # claims beyond that is treated as a manual discount.
        item_dicts = [
            {"product_id": i.product_id, "quantity": i.quantity, "unit_price": i.unit_price} for i in sale_in.items
        ]
        gross = sum(i["quantity"] * i["unit_price"] for i in item_dicts)
        verified_offer = 0.0
        if sale_in.offer_discount > 0:
            fired = await evaluate_preview(db, lines=await build_lines(db, items=item_dicts))
            verified_offer = min(sale_in.offer_discount, round(sum(a for _r, a in fired), 2))
        verified_coupon = 0.0
        coupon_row = None
        coupon_customer = await _find_customer(db, sale_in.customer_phone)
        if sale_in.coupon_code and sale_in.coupon_discount > 0:
            try:
                coupon_row, server_coupon = await check_coupon(
                    db,
                    code=sale_in.coupon_code,
                    customer_id=coupon_customer.id if coupon_customer else None,
                    cart_subtotal=gross - verified_offer,
                )
                verified_coupon = min(sale_in.coupon_discount, server_coupon)
            except HTTPException as exc:
                # Offline-built bill whose coupon no longer holds (expired or
                # used up meanwhile): the goods are sold, so keep the sale,
                # drop the coupon's exemption and flag it for review.
                coupon_row = None
                db.add(
                    SyncFailure(
                        device_id=sale_in.device_id,
                        payload={"sale_id": str(sale_in.id), "coupon_code": sale_in.coupon_code},
                        error=f"coupon_not_honoured: {exc.detail}",
                        resolved=False,
                    )
                )
        manual_discount = max(discount_total - verified_offer - verified_coupon, 0.0)

        # Role-based discount limit; above it requires a manager override PIN, stamped
        # on the sale immediately rather than a pending-approval round trip, because
        # the customer is standing at the till.
        max_allowed = max(float(role.max_discount_value), subtotal * float(role.max_discount_percent) / 100)
        if manual_discount > max_allowed and sale_in.override_user_id is None:
            await savepoint.rollback()
            return SyncItemVerdict(
                client_id=sale_in.id,
                verdict="rejected",
                error="Discount exceeds role limit; manager override required",
            )
        override_user_id = None
        override_reason = None
        if manual_discount > max_allowed:
            overrider = await db.get(User, sale_in.override_user_id)
            if overrider is None:
                await savepoint.rollback()
                return SyncItemVerdict(client_id=sale_in.id, verdict="rejected", error="Unknown override user")
            overrider_role = await db.get(Role, overrider.role_id)
            if float(overrider_role.max_discount_value) < manual_discount and float(
                overrider_role.max_discount_percent
            ) / 100 * subtotal < manual_discount:
                await savepoint.rollback()
                return SyncItemVerdict(
                    client_id=sale_in.id, verdict="rejected", error="Override user lacks sufficient discount authority"
                )
            override_user_id = overrider.id
            override_reason = sale_in.override_reason

        customer = await _get_or_create_customer(db, sale_in.customer_phone)

        # Point 4 audit fix: nothing previously validated that payments summed
        # to the bill amount at all — "split amounts total exactly to bill
        # amount" / "partial/overpayment is prevented" were unenforced. Loyalty
        # redemption is treated like a tender here too (it reduces what's owed
        # via other payment modes) rather than being a ledger-only side effect
        # that left the customer still owing the full amount.
        redeemed_value = 0.0
        if sale_in.loyalty_points_redeemed > 0:
            if customer is None:
                raise ValueError("Loyalty redemption requires a customer")
            config = await get_config(db)
            current_balance = await get_balance(db, customer_id=customer.id)
            if current_balance < float(config.min_balance_to_redeem):
                raise ValueError(
                    f"Customer loyalty balance {current_balance} is below the minimum "
                    f"{float(config.min_balance_to_redeem)} points required to redeem"
                )
            redeemed_value = round(sale_in.loyalty_points_redeemed * float(config.redeem_value), 2)
            if grand_total > 0 and redeemed_value > float(grand_total) * float(config.max_redeem_share):
                raise ValueError(
                    f"Loyalty redemption value {redeemed_value} exceeds the "
                    f"{float(config.max_redeem_share) * 100:.0f}% cap on this bill"
                )

        amount_due = round(float(grand_total) - redeemed_value, 2)
        tendered = round(sum(p.amount for p in sale_in.payments), 2)
        if abs(tendered - amount_due) > 0.01:
            raise ValueError(f"Payments total {tendered} does not match amount due {amount_due} (grand total {grand_total} less loyalty redemption {redeemed_value})")

        sale = Sale(
            id=sale_in.id,
            store_id=sale_in.store_id,
            device_id=sale_in.device_id,
            bill_number=sale_in.bill_number,
            cashier_id=sale_in.cashier_id,
            customer_id=customer.id if customer else None,
            subtotal=subtotal,
            discount_total=discount_total,
            tax_total=tax_total,
            grand_total=grand_total,
            override_user_id=override_user_id,
            override_reason=override_reason,
            place_of_supply=place_of_supply,
            customer_gstin=sale_in.customer_gstin,
            client_idempotency_key=sale_in.client_idempotency_key,
            billed_at=sale_in.billed_at,
        )
        db.add(sale)
        await db.flush()

        # Log what actually fired, so Offer Profit and Coupon Use see till bills
        # the same way they see online orders. Idempotent under replay.
        if verified_offer > 0:
            await evaluate_and_log(
                db,
                store_id=sale_in.store_id,
                lines=await build_lines(db, items=item_dicts),
                source_type="sale",
                source_id=sale.id,
            )
        if coupon_row is not None and verified_coupon > 0:
            db.add(
                CouponRedemption(
                    coupon_id=coupon_row.id,
                    customer_id=customer.id if customer else None,
                    source_type="sale",
                    source_id=sale.id,
                    discount_amount=verified_coupon,
                )
            )
            await db.flush()

        if override_user_id is not None:
            # Point 13 audit fix: a manager discount override was stamped
            # onto the Sale row but never passed through write_audit() —
            # invisible in the Audit Trail screen even though it's exactly
            # the POS exception event fraud.py's primary rule reads.
            await write_audit(
                db,
                user_id=sale_in.cashier_id,
                role_code=role.code,
                store_id=sale_in.store_id,
                device_id=sale_in.device_id,
                action="sale.discount_override",
                entity_type="sale",
                entity_id=sale.id,
                new_value={
                    "discount_total": discount_total,
                    "override_user_id": str(override_user_id),
                    "override_reason": override_reason,
                },
                reason=override_reason,
            )

        for idx, item in enumerate(sale_in.items):
            line_taxable, cgst, sgst, igst = line_tax_breakdown[idx]
            db.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=item.product_id,
                    product_name_snapshot=item.product_name_snapshot,
                    tax_rate_snapshot=item.tax_rate_snapshot,
                    hsn_code_snapshot=item.hsn_code_snapshot,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    line_discount=item.line_discount,
                    line_total=item.quantity * item.unit_price - item.line_discount,
                    taxable_value=line_taxable,
                    cgst_amount=cgst,
                    sgst_amount=sgst,
                    igst_amount=igst,
                    mrp_snapshot=item.mrp_snapshot,
                )
            )
            try:
                await apply_movement(
                    db,
                    product_id=item.product_id,
                    store_id=sale_in.store_id,
                    delta=-item.quantity,
                    reason_code="sale",
                    source_type="sale_item",
                    source_id=sale.id,
                    created_by=sale_in.cashier_id,
                    device_id=sale_in.device_id,
                )
                # A sale is a fact — the goods physically left the shop — so it always
                # applies even if it drives stock negative; negative stock is flagged,
                # never blocked, and surfaced for review.
                resulting = await get_stock_balance(db, product_id=item.product_id, store_id=sale_in.store_id)
                if resulting < 0:
                    db.add(
                        SyncFailure(
                            device_id=sale_in.device_id,
                            payload={
                                "sale_id": str(sale.id),
                                "product_id": str(item.product_id),
                                "resulting_quantity": resulting,
                            },
                            error="negative_stock_flagged_for_review",
                            resolved=False,
                        )
                    )
            except DuplicateMovement:
                pass  # already applied for this sale — safe to ignore under replay

        for payment in sale_in.payments:
            db.add(Payment(sale_id=sale.id, mode=payment.mode, amount=payment.amount, reference=payment.reference))

            # Point 4 audit fix: wallet and gift_voucher were listed as payment
            # modes but nothing actually debited the underlying balance — a
            # "wallet" payment used to just be a string with no money moved.
            if payment.mode == "wallet":
                if customer is None:
                    raise ValueError("Wallet payment requires a customer")
                try:
                    await debit_wallet(
                        db,
                        customer_id=customer.id,
                        amount=payment.amount,
                        reference_type="pos_sale",
                        source_type="sale_payment",
                        source_id=sale.id,
                    )
                except InsufficientWalletBalance as exc:
                    raise ValueError(str(exc)) from exc
                except DuplicateWalletEntry:
                    pass
            elif payment.mode == "gift_voucher":
                if not payment.reference:
                    raise ValueError("Gift voucher payment requires the voucher code as the payment reference")
                voucher = (
                    await db.execute(select(GiftVoucher).where(GiftVoucher.code == payment.reference))
                ).scalar_one_or_none()
                if voucher is None:
                    raise ValueError(f"No gift voucher found for code {payment.reference}")
                if voucher.status != "active":
                    raise ValueError(f"Gift voucher {payment.reference} is {voucher.status}, not active")
                if payment.amount > float(voucher.balance):
                    raise ValueError(
                        f"Gift voucher {payment.reference} balance ₹{float(voucher.balance):.2f} is insufficient for ₹{payment.amount:.2f}"
                    )
                voucher.balance = float(voucher.balance) - payment.amount
                if voucher.balance <= 0:
                    voucher.status = "redeemed"

        # Loyalty: earn on net billed value; redeem against the till's cached balance.
        # Offline the cached balance may be stale — redemption still applies and a
        # resulting negative balance is flagged for review rather than blocked at
        # the counter (a rare write-off beats refusing a customer at checkout).
        if customer:
            config = await get_config(db)
            multiplier = await get_earn_multiplier(db, customer_id=customer.id)
            earn_points = round(float(grand_total) * float(config.earn_rate) * multiplier, 2)
            if earn_points > 0:
                try:
                    await apply_ledger_entry(
                        db,
                        customer_id=customer.id,
                        delta_points=earn_points,
                        reason="earn",
                        source_type="sale",
                        source_id=sale.id,
                    )
                    sale.loyalty_points_earned = earn_points
                except DuplicateLedgerEntry:
                    pass

            if sale_in.loyalty_points_redeemed > 0:
                # min_balance_to_redeem / max_redeem_share / amount-due-vs-
                # tendered were already validated up front, before any write —
                # this just applies the ledger entry now that we know it's valid.
                try:
                    await apply_ledger_entry(
                        db,
                        customer_id=customer.id,
                        delta_points=-sale_in.loyalty_points_redeemed,
                        reason="redeem",
                        source_type="sale",
                        source_id=sale.id,
                    )
                    sale.loyalty_points_redeemed = sale_in.loyalty_points_redeemed
                    balance = await get_balance(db, customer_id=customer.id)
                    if balance < 0:
                        db.add(
                            SyncFailure(
                                device_id=sale_in.device_id,
                                payload={"sale_id": str(sale.id), "customer_id": str(customer.id), "balance": balance},
                                error="negative_loyalty_balance_flagged_for_review",
                                resolved=False,
                            )
                        )
                except DuplicateLedgerEntry:
                    pass

        await write_audit(
            db,
            user_id=sale_in.cashier_id,
            role_code=role.code,
            store_id=sale_in.store_id,
            device_id=sale_in.device_id,
            action="sale.created",
            entity_type="sale",
            entity_id=sale.id,
            new_value={"grand_total": float(grand_total), "bill_number": sale_in.bill_number},
            source="sync",
        )

        await savepoint.commit()
        return SyncItemVerdict(client_id=sale_in.id, verdict="applied", server_id=sale.id)

    except IntegrityError:
        await savepoint.rollback()
        # Most likely the unique (store_id, device_id, bill_number) constraint —
        # two tills should never collide, so surface this as a conflict for review.
        return SyncItemVerdict(client_id=sale_in.id, verdict="conflict", error="Duplicate or conflicting bill_number")
    except ValueError as exc:
        await savepoint.rollback()
        return SyncItemVerdict(client_id=sale_in.id, verdict="rejected", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — quarantine, never let one bad row break the batch
        await savepoint.rollback()
        db.add(SyncFailure(device_id=sale_in.device_id, payload=sale_in.model_dump(mode="json"), error=str(exc)))
        return SyncItemVerdict(client_id=sale_in.id, verdict="rejected", error="Internal error — quarantined")
