"""Creates one extra test user per non-super-admin role, for manual role testing.
Run after scripts.seed: `python -m scripts.seed_test_users`
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.models import Role, Store, User, UserStore

TEST_PASSWORD = "Test@123"

TEST_USERS = [
    ("admin.test@gropto.local", "Test Admin", "admin"),
    ("manager@gropto.local", "Test Store Manager", "store_manager"),
    ("cashier@gropto.local", "Test Cashier", "cashier"),
]


async def main() -> None:
    async with SessionLocal() as db:
        store = (await db.execute(select(Store))).scalars().first()
        if store is None:
            print("No store found — run scripts.seed first.")
            return

        for email, full_name, role_code in TEST_USERS:
            existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if existing:
                print(f"Already exists: {email}")
                continue

            role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one()
            user = User(email=email, full_name=full_name, password_hash=hash_password(TEST_PASSWORD), role_id=role.id)
            db.add(user)
            await db.flush()
            db.add(UserStore(user_id=user.id, store_id=store.id))
            print(f"Created: {email} / {TEST_PASSWORD}  (role={role_code})")

        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
