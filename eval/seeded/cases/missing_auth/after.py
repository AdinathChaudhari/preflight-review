"""Order endpoints."""
from app_core import current_user, db, require_role, abort


@require_role("admin")
def get_order(order_id):
    user = current_user()
    if user is None:
        abort(401)
    return db.fetch_one("SELECT * FROM orders WHERE id = %s", (order_id,))


def delete_order(order_id):
    result = db.execute("DELETE FROM orders WHERE id = %s", (order_id,))
    return {"deleted": order_id, "rows": result}
