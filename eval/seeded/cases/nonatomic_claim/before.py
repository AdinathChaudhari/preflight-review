"""Job queue worker."""
from app_core import db


def claim_job(worker_id):
    row = db.fetch_one(
        "UPDATE jobs SET worker = %s, status = 'running' "
        "WHERE id = (SELECT id FROM jobs WHERE status = 'queued' "
        "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id",
        (worker_id,),
    )
    return row[0] if row else None
