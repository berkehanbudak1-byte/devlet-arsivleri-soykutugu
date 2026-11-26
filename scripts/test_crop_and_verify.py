import json
import subprocess
import sys
import unittest
from pathlib import Path

from crop_and_verify import build_archive_queries, build_lineage_mermaid, hicri_miladi_cevir


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

    def test_build_archive_queries_variants_and_dates(self):
        res = build_archive_queries(name="Mehmedoğlu", place="Muğla", year=1293)
        self.assertIn("Mehmedoğlu", res["name_variants"])
        self.assertIn("Mehmedzade", res["name_variants"])
        self.assertIn("veled-i Mehmed", res["name_variants"])
        self.assertIn("Mugla", res["place_variants"])
        self.assertIn("Mukla", res["place_variants"])
        self.assertIsNotNone(res["date_window"])
        self.assertEqual(res["date_window"]["onerilen_miladi_aralik"], "1871-1882")
        self.assertTrue(len(res["queries"]) >= 4)

    def test_build_archive_queries_validation(self):
        with self.assertRaises(ValueError):
            build_archive_queries(name="", place="")

    def test_cli_query_builder_command(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "query-builder",
                "--name",
                "Çakıroğlu",
                "--place",
                "Üsküdar",
                "--year",
                "1260",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Çakırzade", proc.stdout)
        self.assertIn("Uskudar", proc.stdout)
        self.assertIn("1839-1849", proc.stdout)

    def test_build_lineage_mermaid_pedigree_and_birth_years(self):
        sample_records = [
            {"hane": 14, "isim": "Halil", "baba_adi": "Mustafa", "yas": 52, "hicri_yil": 1260},
            {"hane": 14, "isim": "Mehmed", "baba_adi": "Halil", "yas": 24, "hicri_yil": 1260},
            {"hane": 14, "isim": "Ali", "baba_adi": "Halil", "yas": 18, "hicri_yil": 1260},
        ]
        res = build_lineage_mermaid(sample_records)
        self.assertEqual(len(res["records"]), 3)
        # 1260 Hicri -> 1844 Miladi; Halil (52) -> 1792, Mehmed (24) -> 1820, Ali (18) -> 1826
        self.assertEqual(res["records"][0]["dogum_miladi_hicri"], 1792)
        self.assertEqual(res["records"][1]["dogum_miladi_hicri"], 1820)
        self.assertEqual(res["records"][2]["dogum_miladi_hicri"], 1826)
        self.assertIn("flowchart TD", res["mermaid"])
        self.assertIn("A1 --> P1", res["mermaid"])
        self.assertIn("P1 --> P2", res["mermaid"])
        self.assertIn("P1 --> P3", res["mermaid"])
        self.assertIn("```mermaid", res["markdown"])

    def test_cli_lineage_tree_command(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        payload = json.dumps([
            {"hane": 7, "isim": "Osman", "baba_adi": "İbrahim", "yas": 40, "hicri_yil": 1293},
            {"hane": 7, "isim": "Hasan", "baba_adi": "Osman", "yas": 15, "hicri_yil": 1293},
        ])
        proc = subprocess.run(
            [sys.executable, str(script), "lineage-tree", "--records-json", payload],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("flowchart TD", proc.stdout)
        self.assertIn("1836", proc.stdout)
        self.assertIn("1861", proc.stdout)


if __name__ == "__main__":
    unittest.main()


