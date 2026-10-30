"""File download helper."""
import os

BASE_DIR = "/srv/uploads"


def read_upload(name):
    path = os.path.realpath(os.path.join(BASE_DIR, name))
    if not path.startswith(BASE_DIR + os.sep):
        raise ValueError("path escapes the upload directory")
    with open(path, "rb") as fh:
        return fh.read()
