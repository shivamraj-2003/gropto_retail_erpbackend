"""Point 19: logical backup of the central database, with retention.

    python -m scripts.backup_db                     # dump to ./backups, keep 14 days
    python -m scripts.backup_db --dir D:/backups --keep-days 30

Uses `pg_dump --format=custom` against DATABASE_URL (needs the PostgreSQL
client tools on PATH — https://www.postgresql.org/download/). Run it daily
from Windows Task Scheduler / cron, and copy the backup folder off this
machine (cloud drive, second disk) — a backup on the same disk as nothing
else is still one failure away from gone.

This complements, not replaces, the hosting provider's own backups: check
that the Supabase project has daily backups / point-in-time recovery enabled
(Project Settings -> Database -> Backups).

Restore (into an EMPTY database — never over the live one without a plan):

    pg_restore --clean --if-exists --no-owner --dbname "<postgres URL>" backups/gropto_YYYYmmdd_HHMMSS.dump

then run `python -m alembic current` to confirm the schema revision, and
check a few row counts (stores, products, sales) against expectations.
"""

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from app.core.config import settings


def _libpq_url(url: str) -> str:
    # SQLAlchemy's async driver suffix isn't understood by pg_dump.
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up the Gropto database")
    parser.add_argument("--dir", default="backups", help="output directory (default: ./backups)")
    parser.add_argument("--keep-days", type=int, default=14, help="delete dumps older than this many days")
    args = parser.parse_args()

    if shutil.which("pg_dump") is None:
        print("pg_dump not found on PATH — install the PostgreSQL client tools first.", file=sys.stderr)
        return 2

    out_dir = Path(args.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"gropto_{datetime.now().strftime('%Y%m%d_%H%M%S')}.dump"
    partial = target.with_suffix(".dump.partial")

    result = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(partial), _libpq_url(settings.migration_database_url or settings.database_url)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        partial.unlink(missing_ok=True)
        print(f"Backup FAILED: {result.stderr.strip()}", file=sys.stderr)
        return 1
    partial.rename(target)

    cutoff = time.time() - args.keep_days * 86400
    removed = 0
    for old in out_dir.glob("gropto_*.dump"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
            removed += 1
    print(f"Backup written: {target} ({target.stat().st_size / 1_048_576:.1f} MB); pruned {removed} old dump(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
