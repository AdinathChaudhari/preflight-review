"""backfill tenant ids"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    tenant = os.environ.get("DEFAULT_TENANT")
    if not tenant:
        return
    op.execute(
        sa.text("UPDATE orders SET tenant_id = :tenant WHERE tenant_id IS NULL")
        .bindparams(tenant=tenant)
    )
