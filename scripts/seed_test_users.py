"""Creates one extra test user per non-super-admin role, for manual role testing.
Run after scripts.seed: `python -m scripts.seed_test_users`
"""

import asyncio

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.models import Role, Store, User, UserStore

TEST_PASSWORD = "Test@123"

# store_scoped=True assigns this store's UserStore row (Cashier/Store Manager/
# Regional Manager/Inventory User — roles that are NOT in
# CurrentUser.sees_all_stores()). store_scoped=False leaves the user with zero
# store_ids, which is correct for the enterprise-wide roles (System Admin,
# CEO, COO, Finance Head, Purchase Head) — see app/api/deps.py's
# ENTERPRISE_WIDE_ROLES / sees_all_stores().
TEST_USERS = [
    ("admin.test@gropto.local", "Test Admin", "admin", True),
    ("manager@gropto.local", "Test Store Manager", "store_manager", True),
    ("cashier@gropto.local", "Test Cashier", "cashier", True),
    ("sysadmin@gropto.local", "Test System Admin", "system_admin", False),
    ("ceo@gropto.local", "Test CEO", "ceo", False),
    ("coo@gropto.local", "Test COO", "coo", False),
    ("financehead@gropto.local", "Test Finance Head", "finance_head", False),
    ("purchasehead@gropto.local", "Test Purchase Head", "purchase_head", False),
    ("regionalmgr@gropto.local", "Test Regional Manager", "regional_manager", True),
    ("inventoryuser@gropto.local", "Test Inventory User", "inventory_user", True),
]


async def main() -> None:
    async with SessionLocal() as db:
        store = (await db.execute(select(Store))).scalars().first()
        if store is None:
            print("No store found — run scripts.seed first.")
            return

        for email, full_name, role_code, store_scoped in TEST_USERS:
            existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if existing:
                print(f"Already exists: {email}")
                continue

            role = (await db.execute(select(Role).where(Role.code == role_code))).scalar_one()
            user = User(email=email, full_name=full_name, password_hash=hash_password(TEST_PASSWORD), role_id=role.id)
            db.add(user)
            await db.flush()
            if store_scoped:
                db.add(UserStore(user_id=user.id, store_id=store.id))
            print(f"Created: {email} / {TEST_PASSWORD}  (role={role_code})")

        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
