"""items.created_at"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"


def upgrade():
    op.add_column("items", sa.Column("created_at", sa.DateTime, nullable=True))


def downgrade():
    op.drop_column("items", "created_at")
