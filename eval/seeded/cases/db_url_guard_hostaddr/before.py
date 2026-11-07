"""Test database reset."""
from sqlalchemy.engine import make_url


def reset(engine, url):
    engine.execute("DROP SCHEMA public CASCADE")
