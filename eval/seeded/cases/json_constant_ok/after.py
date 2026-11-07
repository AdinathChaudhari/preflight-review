"""Built-in defaults."""
import json

LIMITS = {"page_size": 50}
_DEFAULTS_JSON = json.dumps({"retries": 3, "timeout_s": 10})


def defaults():
    data = json.loads(_DEFAULTS_JSON)
    return data.get("retries"), data.get("timeout_s")
