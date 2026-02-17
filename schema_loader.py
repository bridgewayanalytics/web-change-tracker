"""
Load Bubble Resource schema field names from schemas/bubble/Resources.csv.

Reads the CSV header row and returns the exact Bubble field names (column headers)
as an ordered list.
"""

import csv
from pathlib import Path

DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schemas" / "bubble" / "Resources.csv"


def load_bubble_resource_fields(csv_path: Path | None = None) -> list[str]:
    """
    Read the Bubble Resources CSV and return column headers as an ordered list.

    Args:
        csv_path: Path to Resources.csv. Defaults to schemas/bubble/Resources.csv.

    Returns:
        List of field names (exact column headers) in order.
    """
    path = csv_path or DEFAULT_SCHEMA_PATH
    if not path.exists():
        return []

    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        row = next(reader, None)
        if row is None:
            return []
        return [h.strip() for h in row if h.strip()]
