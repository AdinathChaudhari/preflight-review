"""Agent wrapper."""
import json
from pathlib import Path

SYSTEM = (Path(__file__).parent.parent / "prompts" / "agent" / "system.md").read_text()


def parse_reply(text):
    return json.loads(text)
