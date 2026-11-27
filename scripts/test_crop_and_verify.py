import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from crop_and_verify import (
    MAX_CROP_ZOOM,
    MAX_IMAGE_PIXELS,
    MAX_OUTPUT_DIMENSION,
    build_archive_queries,
    build_lineage_mermaid,
    compute_safe_crop_box,
    extract_zips_if_needed,
    hicri_miladi_cevir,
    open_image_safely,
    sanitize_filename,
    trace_migration_timeline,
    validate_safe_path,
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


class TestCropAndVerifySecurity(unittest.TestCase):
    def test_decompression_bomb_limit_configured_and_enforced(self):
        self.assertEqual(Image.MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)
        self.assertGreater(MAX_IMAGE_PIXELS, 0)
        self.assertLessEqual(MAX_IMAGE_PIXELS, 100_000_000)

        with patch("crop_and_verify.Image.open", side_effect=Image.DecompressionBombError("bomb")):
            with self.assertRaises(ValueError) as ctx:
                open_image_safely(Path("dummy.png"))
        self.assertIn("decompression bomb", str(ctx.exception).lower())

        # Also enforce strict MAX_IMAGE_PIXELS ceiling even between 1x and 2x (where Pillow only warns)
        class _OversizedStub:
            width = 11_000
            height = 10_000
            closed = False

            def close(self):
                self.closed = True

        stub = _OversizedStub()
        with patch("crop_and_verify.Image.open", return_value=stub):
            with self.assertRaises(ValueError) as ctx2:
                open_image_safely(Path("oversized.png"))
        self.assertTrue(stub.closed)
        self.assertIn("decompression bomb", str(ctx2.exception).lower())

    def test_sanitize_filename_strips_traversal_and_unsafe_chars(self):
        self.assertEqual(sanitize_filename("../../etc/passwd"), "etc_passwd")
        self.assertEqual(sanitize_filename("defter:1260*?<>|"), "defter_1260")
        self.assertEqual(sanitize_filename("...\x00\n"), "output")
        self.assertEqual(sanitize_filename("NFS.d 1293_poz_01"), "NFS.d_1293_poz_01")

    def test_validate_safe_path_rejects_traversal_control_chars_and_symlinks(self):
        with self.assertRaises(ValueError):
            validate_safe_path("../outside.png")
        with self.assertRaises(ValueError):
            validate_safe_path("valid\x00evil.png")
        with self.assertRaises(ValueError):
            validate_safe_path("")

        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "base"
            outside = Path(td) / "outside.png"
            base.mkdir()
            outside.write_bytes(b"data")
            with self.assertRaises(ValueError):
                validate_safe_path(outside, base_dir=base)

            link_path = base / "symlink.png"
            os.symlink(outside, link_path)
            with self.assertRaises(ValueError):
                validate_safe_path(link_path, base_dir=base)

            broken_link = base / "broken_symlink.png"
            os.symlink(Path(td) / "nonexistent.png", broken_link)
            with self.assertRaises(ValueError):
                validate_safe_path(broken_link)

    def test_compute_safe_crop_box_bounds(self):
        box = compute_safe_crop_box(1000, 800, "0.1,0.2,0.5,0.6", zoom=2.0)
        self.assertEqual(box, (100, 160, 500, 480, 800, 640))

        # Excessive zoom rejected
        with self.assertRaises(ValueError):
            compute_safe_crop_box(1000, 800, "0.1,0.1,0.5,0.5", zoom=MAX_CROP_ZOOM + 1)
        # Negative coordinates rejected
        with self.assertRaises(ValueError):
            compute_safe_crop_box(1000, 800, "-10,0,100,100", zoom=1.0)
        # NaN / inf rejected
        with self.assertRaises(ValueError):
            compute_safe_crop_box(1000, 800, "0,0,nan,100", zoom=1.0)
        with self.assertRaises(ValueError):
            compute_safe_crop_box(1000, 800, "0,0,100,100", zoom=float("inf"))
        # Exceeding max output dimension rejected
        with self.assertRaises(ValueError):
            compute_safe_crop_box(
                MAX_OUTPUT_DIMENSION,
                2000,
                f"0,0,{MAX_OUTPUT_DIMENSION},1000",
                zoom=2.0,
            )

    def test_extract_zips_blocks_zip_slip_symlinks_and_zip_bomb(self):
        with tempfile.TemporaryDirectory() as td:
            in_dir = Path(td) / "in"
            out_dir = Path(td) / "out"
            in_dir.mkdir()

            # 1) Zip-slip traversal
            slip_zip = in_dir / "slip.zip"
            with zipfile.ZipFile(slip_zip, "w") as zf:
                zf.writestr("../escape.png", b"evil")
            with self.assertRaises(ValueError):
                extract_zips_if_needed(in_dir, out_dir / "e1")
            slip_zip.unlink()

            # 2) Zip symlink entry
            sym_zip = in_dir / "sym.zip"
            with zipfile.ZipFile(sym_zip, "w") as zf:
                zi = zipfile.ZipInfo("link.png")
                zi.external_attr = (0o120777 << 16)
                zf.writestr(zi, "/etc/passwd")
            with self.assertRaises(ValueError):
                extract_zips_if_needed(in_dir, out_dir / "e2")
            sym_zip.unlink()

            # 3) Zip bomb (high compression ratio)
            bomb_zip = in_dir / "bomb.zip"
            with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("zeros.png", b"\x00" * (2 * 1024 * 1024))
            with self.assertRaises(ValueError):
                extract_zips_if_needed(in_dir, out_dir / "e3")
            bomb_zip.unlink()

            # 4) Valid zip with real image succeeds
            img_path = Path(td) / "sample.png"
            Image.new("RGB", (60, 40), color=(200, 180, 140)).save(img_path)
            good_zip = in_dir / "good.zip"
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.write(img_path, arcname="page_01.png")
            extracted = extract_zips_if_needed(in_dir, out_dir / "e4")
            self.assertEqual(len(extracted), 1)
            self.assertEqual(extracted[0].name, "page_01.png")

    def test_cli_crop_and_split_functional_and_traversal_blocked(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "varak_01.png"
            Image.new("RGB", (200, 120), color=(120, 100, 80)).save(src)
            cropped = Path(td) / "evidence.png"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "crop",
                    "--image",
                    str(src),
                    "--box",
                    "0.1,0.1,0.6,0.6",
                    "--zoom",
                    "2.0",
                    "--output",
                    str(cropped),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(cropped.is_file())
            self.assertIn("200x120 px", proc.stdout)

            # Reject path traversal in --output
            bad = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "crop",
                    "--image",
                    str(src),
                    "--box",
                    "0.1,0.1,0.6,0.6",
                    "--output",
                    str(Path(td) / ".." / "escape.png"),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(bad.returncode, 0)

    def test_mermaid_sanitizes_html_and_control_characters(self):
        res = build_lineage_mermaid([
            {"hane": "1", "isim": "<script>alert(1)</script>\nclick P1", "baba_adi": "Ali`test`", "yas": 30, "hicri_yil": 1293}
        ])
        self.assertNotIn("<script>", res["mermaid"])
        self.assertNotIn("`", res["mermaid"])
        self.assertIn("&lt;script&gt;", res["mermaid"])

    def test_dab_helpers_js_static_and_runtime_security(self):
        js_path = Path(__file__).parent / "dab_helpers.js"
        src = js_path.read_text(encoding="utf-8")
        # Static invariants: no unsafe DOM sinks or dynamic code execution
        self.assertNotIn("innerHTML", src)
        self.assertNotIn("outerHTML", src)
        self.assertNotIn("document.write", src)
        self.assertNotIn("eval(", src)
        self.assertNotIn("new Function", src)

        # Runtime tests via Node.js
        node_test = f"""
const assert = require('assert');
const dab = require({json.dumps(str(js_path))});

// Safe same-origin URLs allowed
const ok = dab.assertSafeUrl('/BelgeGoster.aspx?Id=1', 'https://katalog.devletarsivleri.gov.tr/Arama.aspx');
assert.strictEqual(ok, 'https://katalog.devletarsivleri.gov.tr/BelgeGoster.aspx?Id=1');

// javascript:, data:, file:, cross-origin, and control chars rejected
for (const bad of [
  'javascript:alert(1)',
  'JAVASCRIPT:void(0)',
  'data:text/html,<script>alert(1)</script>',
  'file:///etc/passwd',
  'https://evil.example.com/BelgeGoster.aspx',
  'https://katalog.devletarsivleri.gov.tr/BelgeGoster.aspx\\r\\nX-Injected: 1',
]) {{
  assert.throws(() => dab.assertSafeUrl(bad, 'https://katalog.devletarsivleri.gov.tr/Arama.aspx'));
  assert.strictEqual(dab.sanitizeHref(bad, 'https://katalog.devletarsivleri.gov.tr/Arama.aspx'), '');
}}

// Prototype-pollution-safe JSON parsing
const parsed = dab.safeJsonParse('{{"name":"Ali","__proto__":{{"polluted":true}},"constructor":{{"x":1}}}}');
assert.strictEqual(parsed.name, 'Ali');
assert.strictEqual(({{}}).polluted, undefined);
assert.strictEqual(Object.prototype.hasOwnProperty.call(parsed, '__proto__'), false);
assert.strictEqual(Object.prototype.hasOwnProperty.call(parsed, 'constructor'), false);

// Bounded crop validation
globalThis.__DAB_LAST_IMG = {{ naturalWidth: 1000, naturalHeight: 800 }};
assert.throws(() => dab.cropLastImageDataUrl(-0.1, 0, 0.5, 0.5, 1));
assert.throws(() => dab.cropLastImageDataUrl(0, 0, 0.5, 0.5, 10));
assert.throws(() => dab.cropLastImageDataUrl(0, 0, 0, 0.5, 1));
console.log('DAB_HELPERS_SECURITY_OK');
"""
        proc = subprocess.run(["node", "-e", node_test], capture_output=True, text=True, check=True)
        self.assertIn("DAB_HELPERS_SECURITY_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
