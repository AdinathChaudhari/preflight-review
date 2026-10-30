"""Job queue worker."""
from app_core import db


def claim_job(worker_id):
    row = db.fetch_one(
        "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
    )
    if row is None:
        return None
    db.execute(
        "UPDATE jobs SET worker = %s, status = 'running' WHERE id = %s",
        (worker_id, row[0]),
    )
    return row[0]
