"""Webhook receiver."""
import json


def parse_event(body):
    return json.loads(body)
