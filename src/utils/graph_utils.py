"""
Utility helpers for Microsoft Graph API.
"""
from datetime import datetime, timezone


def format_graph_datetime(dt: datetime) -> str:
    """
    Format a datetime for use in Graph API OData $filter expressions.

    Example output: 2024-01-15T10:30:01Z

    Equivalent to Service.cs:
        latestMessageTime.Value.AddSeconds(1).ToString("s", CultureInfo.InvariantCulture) + "Z"

    Args:
        dt: datetime (timezone-aware or naive; naive assumed UTC)

    Returns:
        ISO 8601 string in UTC with Z suffix
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
