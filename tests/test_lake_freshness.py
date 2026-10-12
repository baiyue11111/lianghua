"""A single fresh stock must not imply the whole lake is fresh."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from collect import lake


class LakeFreshnessTests(unittest.TestCase):
    def test_latest_date_exposes_code_coverage_and_typical_last_date(self):
        with TemporaryDirectory() as directory:
            daily = Path(directory)
            pq.write_table(pa.table({
                "code": ["000001", "000001", "000002", "000003"],
                "date": ["2026-09-11", "2026-09-18", "2026-09-11", "2026-09-11"],
            }), daily / "2026.parquet")
            with patch.object(lake, "DAILY", str(daily)), \
                 patch.object(lake, "DAILY_GLOB", str(daily / "*.parquet")), \
                 patch.dict(lake._LATEST_COVERAGE_CACHE, {"mtime": None, "profile": None}):
                profile = lake.latest_data_coverage()
            self.assertEqual(profile, {
                "latest_data_date": "2026-09-18",
                "latest_data_stock_count": 1,
                "tracked_stock_count": 3,
                "median_stock_date": "2026-09-11",
            })


if __name__ == "__main__":
    unittest.main()
