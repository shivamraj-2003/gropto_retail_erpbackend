"""Blueprint §19/§14 "Backups": a nightly database dump with a retention
window, and a restore tested before go-live.

Shells out to pg_dump (the correct tool for this — reimplementing dump/restore
in pure Python is how you lose data) rather than a hand-rolled export. Requires
the `postgresql-client` package on the host this runs on (the single Docker
Compose server the architecture plan describes) — install it there, this
script only orchestrates it.

Usage:
    python -m scripts.backup_database                  # dump now, prune old dumps
    python -m scripts.backup_database --restore FILE    # restore FILE into DATABASE_URL

Schedule nightly via cron/systemd timer on the server, e.g.:
    0 2 * * *  cd /app && python -m scripts.backup_database >> /var/log/gropto-backup.log 2>&1

BACKUP_DIR defaults to ./backups (local disk). For real object-storage
retention, point BACKUP_DIR at a mounted bucket path (e.g. an rclone/s3fs
mount) — this script does not itself speak to S3/GCS, deliberately: object
storage credentials and provider choice are an infra decision, not something
to hardcode here.
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "./backups"))
RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "14"))


def _pg_connection_args() -> list[str]:
    """Parses DATABASE_URL (postgresql[+asyncpg]://user:pass@host:port/db) into
    pg_dump/psql CLI args, since those tools don't understand the +asyncpg
    driver suffix SQLAlchemy needs."""
    raw = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    parsed = urlparse(raw)
    os.environ["PGPASSWORD"] = parsed.password or ""
    return [
        "-h", parsed.hostname or "localhost",
        "-p", str(parsed.port or 5432),
        "-U", parsed.username or "postgres",
        "-d", (parsed.path or "/postgres").lstrip("/"),
    ]


def run_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_path = BACKUP_DIR / f"gropto_{timestamp}.dump"

    result = subprocess.run(
        ["pg_dump", *_pg_connection_args(), "-Fc", "-f", str(dump_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"BACKUP FAILED: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Backup written: {dump_path} ({dump_path.stat().st_size / 1024 / 1024:.1f} MB)")
    _prune_old_backups()
    return dump_path


def _prune_old_backups() -> None:
    cutoff = datetime.now().timestamp() - RETENTION_DAYS * 86400
    removed = 0
    for f in BACKUP_DIR.glob("gropto_*.dump"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        print(f"Pruned {removed} backup(s) older than {RETENTION_DAYS} days")


def run_restore(dump_file: str) -> None:
    """Restores into whatever DATABASE_URL points at right now — run this
    against a scratch/staging database, never production, to test a backup."""
    path = Path(dump_file)
    if not path.exists():
        print(f"No such file: {path}", file=sys.stderr)
        sys.exit(1)
    confirm = input(f"This will restore {path} into {os.environ['DATABASE_URL'].split('@')[-1]}. Type 'yes' to continue: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return
    result = subprocess.run(
        ["pg_restore", *_pg_connection_args(), "--clean", "--if-exists", "--no-owner", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"RESTORE FAILED: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Restore complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore", metavar="FILE", help="Restore a .dump file into DATABASE_URL instead of backing up")
    args = parser.parse_args()

    if args.restore:
        run_restore(args.restore)
    else:
        run_backup()
