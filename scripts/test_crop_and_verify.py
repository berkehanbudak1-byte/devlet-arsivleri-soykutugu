import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crop_and_verify import (
    build_archive_queries,
    build_lineage_mermaid,
    hicri_miladi_cevir,
    trace_migration_timeline,
)


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

    def test_trace_migration_timeline_chronology_and_cross_references(self):
        # Girdiler bilerek kronolojik olmayan sırada verilmiştir
        records = [
            {
                "miladi_yil": 1924,
                "kaza": "Çatalca",
                "nahiye": "Merkez",
                "karye": "İnceğiz",
                "olay": "Mübadele İskânı",
                "fon": "BCA 130.16.13.2",
            },
            {
                "hicri_yil": 1260,
                "kaza": "Serfice",
                "nahiye": "Velvendos",
                "karye": "Kozani",
                "eski_ad": "Kozana",
                "kisi": "Halil bin Mustafa",
                "olay": "Temettuat Tahriri",
                "fon": "ML.VRD.TMT.d",
            },
            {
                "rumi_yil": 1293,
                "kaza": "Manastır",
                "nahiye": "Velvendos",
                "karye": "Kozani",
                "eski_ad": "Kozana",
                "olay": "Kaza bağ değişikliği",
                "fon": "NFS.d",
            },
        ]
        res = trace_migration_timeline(records)
        self.assertEqual([r["miladi_yil"] for r in res["timeline"]], [1844, 1877, 1924])
        self.assertEqual(len(res["unique_locations"]), 3)
        self.assertEqual(len(res["transitions"]), 2)
        self.assertEqual(res["transitions"][0]["type"], "IDARI_BAG_DEGISIKLIGI")
        self.assertEqual(res["transitions"][1]["type"], "GOC_HAREKETI")
        self.assertTrue(any(cr["type"] == "IDARI_BAG_DEGISIKLIGI" and cr["karye"] == "Kozani" for cr in res["cross_references"]))
        self.assertTrue(any(cr["type"] == "ESKI_AD_ESLESMESI" and cr["eski_ad"] == "Kozana" for cr in res["cross_references"]))
        self.assertTrue(res["mermaid"].startswith("flowchart LR"))
        self.assertIn("L1 -->|\"", res["mermaid"])
        self.assertIn("L2 -->|\"", res["mermaid"])
        self.assertIn("```mermaid", res["markdown"])

    def test_trace_migration_timeline_validation(self):
        with self.assertRaises(ValueError):
            trace_migration_timeline([])
        with self.assertRaises(ValueError):
            trace_migration_timeline([{"miladi_yil": 1850}])

    def test_cli_migration_map_command(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        sample = [
            {"hicri_yil": 1260, "kaza": "Üsküdar", "nahiye": "Kartal", "karye": "Yakacık", "fon": "ML.VRD.TMT.d"},
            {"miladi_yil": 1882, "kaza": "Kartal", "nahiye": "Merkez", "karye": "Yakacık", "fon": "NFS.d"},
        ]
        payload = json.dumps(sample)
        proc = subprocess.run(
            [sys.executable, str(script), "migration-map", "--records-json", payload],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("flowchart LR", proc.stdout)
        self.assertIn("Yakacık", proc.stdout)
        self.assertIn("1844", proc.stdout)

        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            proc_file = subprocess.run(
                [sys.executable, str(script), "migration-map", "--records-json", str(tmp_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("flowchart LR", proc_file.stdout)
        finally:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
