"""Create a fresh database from the bootstrap schema and mark every migration applied."""
from pathlib import Path

SCHEMA = Path(__file__).parent / "schema.sql"


def bootstrap(conn, migration_ids):
    conn.executescript(SCHEMA.read_text())
    conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY)")
    for mid in migration_ids:
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (mid,))
