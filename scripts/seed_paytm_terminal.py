"""Map the Paytm machine in PAYTM_MID / PAYTM_TID / PAYTM_DEVICE_SERIAL to a store.

    python -m scripts.seed_paytm_terminal --store-code ST001 [--label "Counter 1"]

Safe to run again: it updates the same machine. MID and TID are identifiers, not credentials; the merchant key is
never read or written here.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.models import Store
from app.models.models_paytm import PaytmTerminal


async def main(store_code: str, label: str | None) -> None:
    if not (settings.paytm_mid and settings.paytm_tid):
        raise SystemExit("Set PAYTM_MID and PAYTM_TID in the environment first")
    async with SessionLocal() as db:
        store = await db.scalar(select(Store).where(Store.code == store_code))
        if store is None:
            raise SystemExit(f"No store with code {store_code}")
        row = await db.scalar(select(PaytmTerminal).where(PaytmTerminal.mid == settings.paytm_mid, PaytmTerminal.tid == settings.paytm_tid))
        if row is not None and row.store_id != store.id:
            raise SystemExit("That machine is already mapped to another store")
        if row is None:
            row = PaytmTerminal(store_id=store.id, mid=settings.paytm_mid, tid=settings.paytm_tid)
            db.add(row)
        row.device_serial = settings.paytm_device_serial or row.device_serial
        row.label = label or row.label
        row.is_active = True
        await db.commit()
        print(f"Paytm machine {row.tid} is mapped to {store.code} ({store.name})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--store-code", required=True)
    parser.add_argument("--label")
    args = parser.parse_args()
    asyncio.run(main(args.store_code, args.label))
