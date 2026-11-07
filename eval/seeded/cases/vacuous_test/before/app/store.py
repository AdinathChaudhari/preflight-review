class Store:
    def __init__(self):
        self.rows = {}

    def put(self, key, value):
        self.rows[key] = value

    def get(self, key):
        return self.rows.get(key)
