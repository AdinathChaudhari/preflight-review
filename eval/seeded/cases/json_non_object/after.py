"""Webhook receiver."""
import json


def parse_event(body):
    return json.loads(body)


def event_id(body):
    data = json.loads(body)
    return data.get("id")
