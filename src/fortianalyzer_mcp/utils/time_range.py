"""Time-range parsing shared by FAZ MCP tools.

FortiAnalyzer's logview/fortiview/incident/report APIs accept a time-range
dict ``{"start": "YYYY-MM-DD HH:MM:SS", "end": "YYYY-MM-DD HH:MM:SS"}``.
The timestamps are naive (no TZ marker) and FAZ interprets them in its
own system timezone. If the client's system timezone differs from the
FAZ system timezone, relative ranges like "1-hour" silently miss real
logs (see GitHub issue for the discovery story).

This module is the single source of truth for translating either a
preset key ("1-hour", "5-min", ...) or a custom range ("start|end") into
that dict. When a ``faz_tz`` is supplied, "now" is computed in UTC and
converted to the FAZ timezone before being stripped to naive — so the
caller's wall clock no longer matters.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

# Preset relative ranges. Keys are caller-facing strings; values are
# the size of the window ending at "now". Order roughly small -> large.
_RANGE_MAP: dict[str, timedelta] = {
    "now": timedelta(minutes=5),  # alias for 5-min, used by FortiView "real-time"
    "5-min": timedelta(minutes=5),
    "15-min": timedelta(minutes=15),
    "30-min": timedelta(minutes=30),
    "1-hour": timedelta(hours=1),
    "2-hour": timedelta(hours=2),
    "6-hour": timedelta(hours=6),
    "12-hour": timedelta(hours=12),
    "24-hour": timedelta(hours=24),
    "1-day": timedelta(days=1),
    "2-day": timedelta(days=2),
    "7-day": timedelta(days=7),
    "30-day": timedelta(days=30),
    "90-day": timedelta(days=90),
}

# Public for documentation / docstrings in callers.
SUPPORTED_TIME_RANGES: tuple[str, ...] = tuple(_RANGE_MAP.keys())

_DYNAMIC_RE = re.compile(
    r"^(\d+(?:\.\d+)?)-"
    r"(min(?:ute)?s?|hour?s?|day?s?|week?s?|month?s?)$",
    re.IGNORECASE,
)

_UNIT_SECONDS: dict[str, int] = {
    "min": 60, "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
    "month": 2592000, "months": 2592000,
}


def _parse_dynamic_range(time_range: str) -> dict[str, str] | None:
    """Parse a free-form N-unit time range string like '3.5-day' or '90-min'.

    Returns a FAZ-compatible time dict or None if not parseable.
    """
    m = _DYNAMIC_RE.match(time_range.strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower().rstrip("s")
    seconds = _UNIT_SECONDS.get(unit) or _UNIT_SECONDS.get(unit + "s")
    if seconds is None:
        return None
    total_seconds = int(value * seconds)
    # Return as a relative range dict that FAZ understands
    return {"range": f"-{total_seconds}", "unit": "second"}


def parse_time_range(
    time_range: str,
    faz_tz: ZoneInfo | None = None,
    anchor: datetime | None = None,
) -> dict[str, str]:
    """Translate a caller-facing time-range string to FAZ's dict format.

    Args:
        time_range: Either a preset key from :data:`SUPPORTED_TIME_RANGES`
            (e.g. ``"1-hour"``, ``"5-min"``, ``"7-day"``) or a custom
            range ``"YYYY-MM-DD HH:MM:SS|YYYY-MM-DD HH:MM:SS"``.
        faz_tz: FAZ system timezone (recommended). When provided, "now"
            is taken in UTC and converted to ``faz_tz`` before being
            stripped to naive form. When ``None``, falls back to
            :func:`datetime.now` (caller-local) — preserves legacy
            behavior, but only correct if the caller and FAZ happen
            to share a timezone.
        anchor: Explicit naive FAZ-local "now" to end relative windows on.
            When provided it takes precedence over ``faz_tz``/``datetime.now``
            — used to align relative ranges to the appliance's LogView ingest
            clock (see :mod:`fortianalyzer_mcp.utils.log_clock`) so a post-
            upgrade clock skew does not silently miss recent logs. Ignored for
            custom ``"start|end"`` ranges (those carry explicit timestamps).

    Returns:
        ``{"start": "...", "end": "..."}`` ready to send as
        ``time-range`` in a logview/fortiview/incident API call.

    Raises:
        ValueError: If ``time_range`` is neither a known preset key nor
            a valid custom ``"start|end"`` range. The previous behavior
            of silently falling back to ``1-hour`` for unknown keys hid
            typos; callers now get a hard failure they can fix.
    """
    if "|" in time_range:
        parts = time_range.split("|", maxsplit=1)
        start = parts[0].strip()
        end = parts[1].strip()
        if not start or not end:
            raise ValueError(
                f"Custom time_range must be 'start|end' with non-empty parts, got {time_range!r}"
            )
        try:
            start_dt = datetime.strptime(start, _TIMESTAMP_FMT)
            end_dt = datetime.strptime(end, _TIMESTAMP_FMT)
        except ValueError as exc:
            raise ValueError(
                f"Custom time_range timestamps must be 'YYYY-MM-DD HH:MM:SS', got {time_range!r}"
            ) from exc
        if start_dt > end_dt:
            raise ValueError(f"Custom time_range start must be <= end, got {time_range!r}")
        return {"start": start, "end": end}

    delta = _RANGE_MAP.get(time_range)
    if delta is None:
        dynamic = _parse_dynamic_range(time_range)
        if dynamic is not None:
            return dynamic
        raise ValueError(
            f"Unknown time_range {time_range!r}. Valid presets: "
            f"{', '.join(SUPPORTED_TIME_RANGES)}. Or use a custom range "
            f"'YYYY-MM-DD HH:MM:SS|YYYY-MM-DD HH:MM:SS'."
        )

    if anchor is not None:
        # Explicit FAZ-local anchor (e.g. latest LogView ingest time). Strip any
        # tzinfo so the bytes-on-wire stay naive FAZ-local like every other path.
        now = anchor.replace(tzinfo=None)
    elif faz_tz is not None:
        now = datetime.now(UTC).astimezone(faz_tz).replace(tzinfo=None)
    else:
        now = datetime.now()

    start_dt = now - delta
    return {
        "start": start_dt.strftime(_TIMESTAMP_FMT),
        "end": now.strftime(_TIMESTAMP_FMT),
    }


def parse_time_range_bounds(time_range: dict[str, str]) -> tuple[datetime, datetime]:
    """Parse a FAZ time-range dict back into ``datetime`` bounds.

    Used by traffic-analysis tools that need to slice a window into
    sub-ranges (see ``traffic_tools._build_bounded_time_slices``).
    """
    return (
        datetime.strptime(time_range["start"], _TIMESTAMP_FMT),
        datetime.strptime(time_range["end"], _TIMESTAMP_FMT),
    )
