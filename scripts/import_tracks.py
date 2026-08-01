"""Import exported song tracks into the deployed viberadio database.

Runs on the Docker host, against the bind-mounted data directory. Idempotent: a
track already in the database, or one whose audio never made it across, is skipped.

Usage: python3 scripts/import_tracks.py <data_dir> <tracks.json>
"""

import json
import sqlite3
import sys
from pathlib import Path

CONTAINER_DATA = "/app/backend/data"

data_dir = Path(sys.argv[1]).resolve()
rows = json.loads(Path(sys.argv[2]).read_text())

con = sqlite3.connect(data_dir / "viberadio.db")
existing = {r[0] for r in con.execute("select file_path from tracks")}

added = skipped = missing = 0
for r in rows:
    if r["file_path"] in existing:
        skipped += 1
        continue
    # The row is useless without the normalized audio the sync should have brought.
    host_path = Path(r["normalized_path"].replace(CONTAINER_DATA, str(data_dir)))
    if not host_path.exists():
        print(f"  missing audio, skipping: {r['artist']} — {r['title']}")
        missing += 1
        continue
    con.execute(
        "insert into tracks"
        " (kind,title,artist,file_path,normalized_path,duration_sec,source_url,created_at)"
        " values (?,?,?,?,?,?,?,?)",
        (
            r["kind"],
            r["title"],
            r["artist"],
            r["file_path"],
            r["normalized_path"],
            r["duration_sec"],
            r["source_url"],
            r["created_at"],
        ),
    )
    existing.add(r["file_path"])
    added += 1

con.commit()
total = con.execute("select count(*) from tracks").fetchone()[0]
print(f"added={added} skipped={skipped} missing={missing} total_tracks={total}")
