"""Settings."""
import os

from dotenv import load_dotenv

load_dotenv(".env.local")
API_URL = os.environ.get("API_URL")
