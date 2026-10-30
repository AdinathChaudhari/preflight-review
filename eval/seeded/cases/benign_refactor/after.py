"""Order totals."""


def _subtotal(items):
    total = 0
    for item in items:
        total += item["price"] * item["qty"]
    return total


def order_total(items, tax_rate):
    return round(_subtotal(items) * (1 + tax_rate), 2)


def label(name):
    return name.strip().title()
