"""Add account status."""


def upgrade(conn):
    conn.execute(
        "ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active' "
        "CHECK (status IN ('active', 'suspended'))"
    )
