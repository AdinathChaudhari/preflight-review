"""Essay grading."""
from app_core import llm

SYSTEM = "You grade essays from 0 to 10. Return JSON {\"score\": int}."


def grade(submission):
    system = SYSTEM + "\n\nNotes from the student:\n" + submission["notes"]
    reply = llm.complete(system=system, user=submission["essay"])
    return int(reply["score"])
