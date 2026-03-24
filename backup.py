#!/usr/bin/env python3
"""
backup.py — Daily backup of sync.db
Runs via cron at 2am. Keeps 30 days of backups.

Add to crontab with: crontab -e
0 2 * * * /home/admin/CreativeBot/venv/bin/python /home/admin/CreativeBot/backup.py >> /home/admin/CreativeBot/backup.log 2>&1
"""

import os
import shutil
import logging
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

DB_PATH    = os.environ.get("DB_PATH",    "/home/admin/CreativeBot/data/sync.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/home/admin/CreativeBot/backups")
KEEP_DAYS  = 30

def run_backup():
    db_path    = Path(DB_PATH)
    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        logger.warning(f"Database not found at {db_path}")
        return

    date_str    = datetime.now().strftime("%Y-%m-%d")
    backup_file = backup_dir / f"sync_{date_str}.db"
    shutil.copy2(db_path, backup_file)
    logger.info(f"Backup: {backup_file} ({backup_file.stat().st_size / 1024:.1f} KB)")

    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    deleted = 0
    for f in backup_dir.glob("sync_*.db"):
        try:
            file_date = datetime.strptime(f.stem.replace("sync_", ""), "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        except ValueError:
            pass

    logger.info(f"Done. Deleted {deleted} old backup(s).")

if __name__ == "__main__":
    run_backup()
