"""File download helper."""
import os

BASE_DIR = "/srv/uploads"


def read_upload(name):
    path = os.path.join(BASE_DIR, name)
    with open(path, "rb") as fh:
        return fh.read()
