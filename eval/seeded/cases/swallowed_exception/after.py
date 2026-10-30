"""Appointment sync."""
import logging

log = logging.getLogger(__name__)


def sync_appointment(api, appt):
    try:
        api.push(appt)
    except Exception:
        pass
    return True
