import subprocess
import sys
import unittest
from pathlib import Path

from crop_and_verify import hicri_miladi_cevir


class TestCropAndVerifyCalendar(unittest.TestCase):
    def test_temettuat_1260(self):
        res = hicri_miladi_cevir(1260)
        self.assertEqual(res["girdi_yili"], 1260)
        self.assertEqual(res["hicri_ise_miladi"], 1844)
        self.assertEqual(res["rumi_ise_miladi"], 1844)
        self.assertIn("Temettuat", res["donem_notu"])

    def test_nufus_1293(self):
        res = hicri_miladi_cevir(1293)
        self.assertEqual(res["hicri_ise_miladi"], 1876)
        self.assertEqual(res["rumi_ise_miladi"], 1877)
        self.assertIn("Sicill-i Nüfus", res["donem_notu"])

    def test_invalid_year(self):
        with self.assertRaises(ValueError):
            hicri_miladi_cevir(0)

    def test_cli_calendar_command(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        proc = subprocess.run(
            [sys.executable, str(script), "calendar", "--year", "1293", "--birth-age", "35"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("1876", proc.stdout)
        self.assertIn("1877", proc.stdout)
        self.assertIn("1841", proc.stdout)


if __name__ == "__main__":
    unittest.main()
