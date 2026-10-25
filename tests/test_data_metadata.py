from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from krabs.borrow import load_borrow_table
from krabs.market.universe import PointInTimeUniverse


class UniverseTests(unittest.TestCase):
    def test_as_of_excludes_future_and_delisted_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.csv"
            path.write_text(
                "symbol,name,listed,delisted,cluster,source\n"
                "AAA,Old,2000-01-03,2021-06-30,test,vendor\n"
                "BBB,New,2024-01-02,,test,vendor\n"
                "CCC,Live,2010-05-03,,test,vendor\n",
                encoding="utf-8",
            )
            universe = PointInTimeUniverse.from_file(path)
            self.assertEqual(universe.as_of(date(2020, 1, 1)), {"AAA", "CCC"})
            self.assertEqual(universe.as_of(date(2022, 1, 1)), {"CCC"})
            self.assertEqual(universe.as_of(date(2024, 6, 1)), {"BBB", "CCC"})

    def test_audit_flags_survivorship_and_missing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe.csv"
            path.write_text(
                "symbol,name,listed,delisted,cluster,source\n"
                "AAA,Old,2000-01-03,2021-06-30,test,vendor\n"
                "BBB,Live,2010-05-03,,test,vendor\n",
                encoding="utf-8",
            )
            info = PointInTimeUniverse.from_file(path).audit(["AAA", "BBB", "CCC"])
            self.assertEqual(info["listings"], 2)
            self.assertEqual(info["delisted"], 1)
            self.assertFalse(info["survivorship_warning"])
            self.assertEqual(info["missing_from_universe"], ["CCC"])


class BorrowTests(unittest.TestCase):
    def test_loads_rates_and_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "borrow.csv"
            path.write_text(
                "symbol,borrow_bps_per_year,available\n"
                "XLK,40,true\n"
                "TINY,2500,false\n",
                encoding="utf-8",
            )
            table = load_borrow_table(path)
            self.assertAlmostEqual(table["XLK"].bps_per_year, 40.0)
            self.assertTrue(table["XLK"].available)
            self.assertFalse(table["TINY"].available)
            self.assertAlmostEqual(table["TINY"].bps_per_year, 2500.0)

    def test_missing_table_keeps_base_rates(self):
        table = load_borrow_table("/nonexistent/borrow.csv", base={"XLK": 25.0})
        self.assertIn("XLK", table)
        self.assertAlmostEqual(table["XLK"].bps_per_year, 25.0)


if __name__ == "__main__":
    unittest.main()
