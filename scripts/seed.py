"""Bootstrap seed: one store, one Super Admin user, one activated device.
Run after migrations: `python -m scripts.seed`
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.models import Device, Role, Store, User, UserStore

SUPER_ADMIN_EMAIL = "admin@gropto.local"
SUPER_ADMIN_PASSWORD = "SuperAdmin@123"


async def main() -> None:
    async with SessionLocal() as db:
        result = await db.execute(select(User).where(User.email == SUPER_ADMIN_EMAIL))
        if result.scalar_one_or_none():
            print("Seed already applied.")
            return

        role = (await db.execute(select(Role).where(Role.code == "super_admin"))).scalar_one()

        store = Store(code="ST001", name="Pilot Store", city="Bengaluru")
        db.add(store)
        await db.flush()

        user = User(
            email=SUPER_ADMIN_EMAIL,
            full_name="Super Admin",
            password_hash=hash_password(SUPER_ADMIN_PASSWORD),
            role_id=role.id,
        )
        db.add(user)
        await db.flush()
        db.add(UserStore(user_id=user.id, store_id=store.id))

        # Every device is created active — login is email+password only,
        # with no pending/approval gate. This bootstrap device is no
        # different from any other device's first login, just seeded ahead
        # of time so `alembic upgrade head` + this script leaves a working
        # login on the first try.
        device = Device(
            store_id=store.id,
            code="TILL1",
            fingerprint="seed-bootstrap-device",
            status="active",
        )
        db.add(device)

        await db.commit()

        print("Seed complete.")
        print(f"  Store:            {store.code} ({store.id})")
        print(f"  Super Admin:      {SUPER_ADMIN_EMAIL} / {SUPER_ADMIN_PASSWORD}")
        print("  Device fingerprint: seed-bootstrap-device (pre-activated)")


if __name__ == "__main__":
    asyncio.run(main())
