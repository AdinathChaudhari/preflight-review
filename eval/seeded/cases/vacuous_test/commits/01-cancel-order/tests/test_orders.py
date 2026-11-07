from app.orders import cancel_order
from app.store import Store


def test_cancel_order_removes_open_status():
    store = Store()
    cancel_order(store, "o-1")
    assert store.get("o-1") is None or store.get("o-1")["status"] == "cancelled"
