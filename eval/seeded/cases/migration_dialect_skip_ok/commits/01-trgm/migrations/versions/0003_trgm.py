"""trigram index (postgres only)"""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return  # the index is a Postgres-only optimisation; nothing to do elsewhere
    op.execute("CREATE INDEX IF NOT EXISTS ix_name_trgm ON items USING gin (name gin_trgm_ops)")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_name_trgm")
