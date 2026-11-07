"""Essay grading."""
from app_core import llm

SYSTEM = "You grade essays from 0 to 10. Return JSON {\"score\": int}."


def grade(submission):
    reply = llm.complete(system=SYSTEM, user=submission["essay"])
    return int(reply["score"])
