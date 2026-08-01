"""Export song tracks from a local viberadio database as JSON for a deployment.

File paths are rewritten from the local checkout to the container layout, since
the database stores them absolute.

Usage: python3 scripts/export_tracks.py <backend_dir> <out.json>
"""

import json
import sqlite3
import sys
from pathlib import Path

CONTAINER_DATA = "/app/backend/data"

backend_dir = Path(sys.argv[1]).resolve()
out = Path(sys.argv[2])

db = backend_dir / "data" / "viberadio.db"
if not db.exists():
    print(f"no database at {db} — nothing to export")
    out.write_text("[]")
    sys.exit(0)

con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
rows = [
    dict(r)
    for r in con.execute(
        "select kind,title,artist,file_path,normalized_path,duration_sec,source_url,created_at"
        " from tracks where kind='song'"
    )
]

local_data = str(backend_dir / "data")
for r in rows:
    for key in ("file_path", "normalized_path"):
        if r[key]:
            r[key] = r[key].replace(local_data, CONTAINER_DATA)

out.write_text(json.dumps(rows))
print(f"exported {len(rows)} song tracks")
