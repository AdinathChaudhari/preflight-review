"""Order operations."""


def create_order(store, order_id):
    store.put(order_id, {"id": order_id, "status": "open"})
