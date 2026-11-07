"""Account queries."""


def suspended_accounts(conn):
    return conn.execute("SELECT id, email FROM accounts WHERE status = 'suspended'").fetchall()
