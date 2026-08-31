#!/usr/bin/env python3
"""WAL-safe daily backup of the hosted DB (same idea as backup.sh, but needs only
Python: uses sqlite3's online backup API, so a consistent snapshot is taken even
mid-write). Keeps the newest KEEP copies in backups/."""
import glob
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "sleep_bandit.db")
OUT = os.path.join(ROOT, "backups")
KEEP = 30

os.makedirs(OUT, exist_ok=True)
dest = os.path.join(OUT, "sleep_bandit-%s.db" % time.strftime("%Y%m%d-%H%M%S"))
src, dst = sqlite3.connect(DB), sqlite3.connect(dest)
with dst:
    src.backup(dst)
src.close(); dst.close()
old = sorted(glob.glob(os.path.join(OUT, "sleep_bandit-*.db")), key=os.path.getmtime, reverse=True)[KEEP:]
for f in old:
    os.remove(f)
print(time.strftime("%Y-%m-%d %H:%M:%S"), "backed up ->", dest, "(keeping newest %d)" % KEEP)
