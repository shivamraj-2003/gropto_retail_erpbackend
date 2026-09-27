"""Bootstrap seed: one store, one Super Admin user, one activated device.
Run after migrations: `python -m scripts.seed`
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.models import Device, Role, Store, User, UserStore

SUPER_ADMIN_EMAIL = "admin@gropto.local"
SUPER_ADMIN_PASSWORD = "ChangeMe!123"


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

        # The bootstrap device is created already-active rather than pending —
        # there is by definition no one logged in yet who could click
        # "approve" on it. Every device after this one goes through the
        # normal pending -> Super Admin approves flow (POST
        # /auth/devices/{id}/approve), same as any device that just shows up
        # and tries to log in for the first time.
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
