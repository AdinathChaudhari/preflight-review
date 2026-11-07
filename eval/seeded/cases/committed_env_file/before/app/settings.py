"""Service settings, read from the process environment."""
import os

API_URL = os.environ.get("API_URL", "http://localhost:8000")
API_KEY = os.environ.get("API_KEY", "")
