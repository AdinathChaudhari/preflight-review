"""Order operations."""


def create_order(store, order_id):
    store.put(order_id, {"id": order_id, "status": "open"})


def cancel_order(store, order_id):
    order = store.get(order_id)
    if order is None:
        return
    order["status"] = "cancelled"
    store.put(order_id, order)
