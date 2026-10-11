"""Pure, point-in-time feature helpers shared by strategy adapters."""

from __future__ import annotations

from datetime import date


def conservative_available_date(report_period: str) -> date:
    """Return the latest statutory-style availability date for a quarter."""
    try:
        year_text, quarter = report_period.split("-")
        year = int(year_text)
    except ValueError as error:
        raise ValueError("report_period must look like YYYY-QN") from error
    if quarter == "Q1":
        return date(year, 5, 1)
    if quarter == "Q2":
        return date(year, 9, 1)
    if quarter == "Q3":
        return date(year, 11, 1)
    if quarter == "Q4":
        return date(year + 1, 5, 1)
    raise ValueError("report_period must end with Q1, Q2, Q3, or Q4")
