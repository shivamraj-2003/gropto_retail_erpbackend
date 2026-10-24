"""CI only: build the whole schema on an EMPTY throwaway Postgres straight from the models, so a broken model, a missing
sequence or a bad constraint fails the pull request.

Why not `alembic upgrade head` from scratch? The first migration creates every table from the models as they are today
(Base.metadata.create_all), so replaying the later migrations on top of it would add columns that already exist. The
production database was built migration by migration and is already current, so CI proves the models build and then
stamps the database at head; the migrations themselves are checked for a single head.
"""

import os
import sys

from sqlalchemy import create_engine, inspect, text

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.database import Base

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg2://")
engine = create_engine(url)
with engine.begin() as conn:
    for ext in ("pgcrypto", "pg_trgm"):
        conn.execute(text(f'create extension if not exists "{ext}"'))
    # sequences that column defaults in the models call nextval() on
    for seq in ("global_revision_seq", "transfer_no_seq"):
        conn.execute(text(f"create sequence if not exists {seq}"))
    Base.metadata.create_all(bind=conn)

tables = inspect(engine).get_table_names()
missing = sorted(set(Base.metadata.tables) - set(tables))
if missing:
    sys.exit(f"tables not created: {missing}")
print(f"schema built from the models: {len(tables)} tables")
