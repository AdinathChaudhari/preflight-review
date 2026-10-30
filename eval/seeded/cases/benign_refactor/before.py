"""Order totals."""


def order_total(items, tax_rate):
    subtotal = 0
    for item in items:
        subtotal += item["price"] * item["qty"]
    return round(subtotal * (1 + tax_rate), 2)


def label(name):
    return name.strip().title()
