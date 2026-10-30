"""Appointment sync."""
import logging

log = logging.getLogger(__name__)


def sync_appointment(api, appt):
    try:
        api.push(appt)
    except ConnectionError as exc:
        log.error("sync failed for %s: %s", appt["id"], exc)
        raise
    return True
