"""Test database reset."""
from sqlalchemy.engine import make_url

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def reset(engine, url):
    parsed = make_url(url)
    if parsed.host not in LOCAL_HOSTS:
        raise RuntimeError(f"refusing to reset non-local database {parsed.host}")
    engine.execute("DROP SCHEMA public CASCADE")
