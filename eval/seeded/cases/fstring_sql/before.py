"""Customer search."""
from app_core import db


def search_customers(name):
    rows = db.fetch_all(
        "SELECT id, name FROM customers WHERE name = %s", (name,)
    )
    return [{"id": r[0], "name": r[1]} for r in rows]


def count_customers():
    return db.fetch_one("SELECT COUNT(*) FROM customers")[0]
