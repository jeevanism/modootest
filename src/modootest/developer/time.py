"""Date and time freezing utilities for modootest tests."""
from contextlib import contextmanager
from typing import Any, Optional


@contextmanager
def freeze_time(time_to_freeze: Any, **kwargs: Any):
    """Context manager freezing system and Odoo ORM time.

    Wraps freezegun.freeze_time if available. Freezes both Python's datetime
    and Odoo's fields.Datetime.now() / fields.Date.today().

    :param time_to_freeze: Target datetime, date, or date string (e.g. '2026-01-01 12:00:00').
    :param kwargs: Optional parameters passed directly to freezegun.freeze_time.
    :return: Context manager yielding the frozen time object.
    """
    try:
        import freezegun
    except ImportError as exc:
        raise RuntimeError(
            "freeze_time requires the optional 'freezegun' package to be installed. "
            "Install it via: pip install freezegun"
        ) from exc

    with freezegun.freeze_time(time_to_freeze, **kwargs) as frozen:
        yield frozen
