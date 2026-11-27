import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, UnidentifiedImageError

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import crop_and_verify
from crop_and_verify import (
    ALLOWED_IMAGE_FORMATS,
    FORBIDDEN_IMAGE_SIGNATURES,
    MAX_CROP_ZOOM,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_OUTPUT_DIMENSION,
    _escape_mermaid_label,
    _harden_pillow,
    _require_pil_image,
    build_archive_queries,
    build_lineage_mermaid,
    compute_safe_crop_box,
    extract_zips_if_needed,
    format_citation_report_markdown,
    hicri_miladi_cevir,
    open_image_safely,
    sanitize_filename,
    sanitize_markdown_cell,
    sanitize_mermaid_label,
    trace_migration_timeline,
    validate_image_magic_bytes,
    validate_safe_path,
    verify_citations,
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
        pil_image = _require_pil_image()
        self.assertEqual(MAX_IMAGE_PIXELS, 16_000_000)
        self.assertEqual(pil_image.MAX_IMAGE_PIXELS, 16_000_000)
        self.assertNotIn("EPS", pil_image.OPEN)
        self.assertNotIn("EPS", pil_image.ID)

        with tempfile.TemporaryDirectory() as td:
            sample_png = Path(td) / "sample.png"
            Image.new("RGB", (20, 20), color=(180, 160, 120)).save(sample_png, format="PNG")

            with patch("PIL.Image.open", side_effect=Image.DecompressionBombError("bomb")):
                with self.assertRaises(ValueError) as ctx:
                    open_image_safely(sample_png)
            self.assertIn("decompression bomb", str(ctx.exception).lower())

            # Also enforce strict MAX_IMAGE_PIXELS ceiling even between 1x and 2x (where Pillow only warns)
            class _OversizedStub:
                width = 11_000
                height = 10_000
                closed = False
                loaded = False

                def load(self):
                    self.loaded = True

                def close(self):
                    self.closed = True

            stub = _OversizedStub()
            with patch("PIL.Image.open", return_value=stub):
                with self.assertRaises(ValueError) as ctx2:
                    open_image_safely(sample_png)
            self.assertTrue(stub.closed)
            self.assertFalse(stub.loaded)
            self.assertIn("decompression bomb", str(ctx2.exception).lower())

            # Real non-mocked decompression bomb enforcement: >2x and 1x..2x warning band
            with self.assertRaises(ValueError) as ctx_over_2x:
                open_image_safely(sample_png, max_pixels=100)
            self.assertIn("decompression bomb", str(ctx_over_2x.exception).lower())

            with self.assertRaises(ValueError) as ctx_band_1x_2x:
                open_image_safely(sample_png, max_pixels=250)
            self.assertIn("decompression bomb", str(ctx_band_1x_2x.exception).lower())

            for bad_limit in (0, -1, True, False):
                with self.assertRaises(ValueError):
                    open_image_safely(sample_png, max_pixels=bad_limit)

            # Real 20 MP (1x..2x warning band of 16 MP) and 36 MP (>2x) 1-bit PNGs rejected without mocks
            png_20mp = Path(td) / "bomb_20mp.png"
            Image.new("1", (5000, 4000), 0).save(png_20mp, format="PNG")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with self.assertRaises(ValueError) as ctx_20mp:
                    open_image_safely(png_20mp)
            self.assertIn("decompression bomb", str(ctx_20mp.exception).lower())

            png_36mp = Path(td) / "bomb_36mp.png"
            Image.new("1", (6000, 6000), 0).save(png_36mp, format="PNG")
            with self.assertRaises(ValueError) as ctx_36mp:
                open_image_safely(png_36mp)
            self.assertIn("decompression bomb", str(ctx_36mp.exception).lower())

    def test_upload2shell_pillow_eps_disabled_and_allowed_formats(self):
        self.assertEqual(ALLOWED_IMAGE_FORMATS, ("PNG", "JPEG", "WEBP", "TIFF"))
        self.assertEqual(MAX_IMAGE_BYTES, 25 * 1024 * 1024)
        self.assertEqual(FORBIDDEN_IMAGE_SIGNATURES, (b"%!PS", b"\xc5\xd0\xd3\xc6", b"%PDF"))

        pil_image = _require_pil_image()
        self.assertNotIn("EPS", pil_image.OPEN)
        self.assertNotIn("EPS", pil_image.ID)

        # Bare PIL.Image.open must also refuse EPS after hardening
        eps_ascii = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n"
        with self.assertRaises(UnidentifiedImageError):
            Image.open(io.BytesIO(eps_ascii))

        # _harden_pillow strips duplicate EPS entries cleanly
        class _FakePil:
            MAX_IMAGE_PIXELS = None
            OPEN = {"PNG": object(), "EPS": object()}
            ID = ["EPS", "PNG", "EPS", "JPEG"]
            inited = False

            @classmethod
            def init(cls):
                cls.inited = True

        _harden_pillow(_FakePil)
        self.assertTrue(_FakePil.inited)
        self.assertEqual(_FakePil.MAX_IMAGE_PIXELS, 16_000_000)
        self.assertNotIn("EPS", _FakePil.OPEN)
        self.assertNotIn("EPS", _FakePil.ID)

        # Valid PNG, JPEG, WEBP, and TIFF archival images open cleanly
        with tempfile.TemporaryDirectory() as td:
            for ext, fmt in (
                (".png", "PNG"),
                (".jpg", "JPEG"),
                (".webp", "WEBP"),
                (".tiff", "TIFF"),
            ):
                p = Path(td) / f"varak{ext}"
                Image.new("RGB", (24, 16), color=(90, 110, 130)).save(p, format=fmt)
                with open_image_safely(p) as im:
                    self.assertEqual(im.size, (24, 16))
                    self.assertEqual(im.format, fmt)

    def test_open_image_safely_rejects_eps_ps_pdf_gif_empty_and_oversized(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            forbidden_payloads = {
                "ascii_eps.jpg": b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n",
                "ws_eps.png": b" \t\r\n%!PS-Adobe-3.0\nshowpage\n",
                "nul_eps.jpg": b"\x00\x00%!PS-Adobe-3.0\nshowpage\n",
                "binary_epsf.tiff": b"\xc5\xd0\xd3\xc6" + b"\x00" * 28,
                "pdf_disguised.jpg": b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n",
                "empty.png": b"",
                "whitespace_only.png": b"   \n\x00\t",
            }
            for fname, payload in forbidden_payloads.items():
                fp = td_path / fname
                fp.write_bytes(payload)
                with self.assertRaises(ValueError, msg=f"{fname} reddedilmeli"):
                    open_image_safely(fp)

            # Disguised GIF saved with .png extension rejected by format allowlist
            gif_as_png = td_path / "disguised_gif.png"
            Image.new("RGB", (16, 16), color=(255, 0, 0)).save(gif_as_png, format="GIF")
            with self.assertRaises(ValueError) as ctx_gif:
                open_image_safely(gif_as_png)
            self.assertIn("format", str(ctx_gif.exception).lower())

            # Exceeding max_bytes rejected before Pillow decode
            valid_png = td_path / "valid.png"
            Image.new("RGB", (32, 32), color=(40, 80, 120)).save(valid_png, format="PNG")
            self.assertGreater(valid_png.stat().st_size, 16)
            with self.assertRaises(ValueError):
                open_image_safely(valid_png, max_bytes=16)

            for bad_max_bytes in (0, -5, True, False):
                with self.assertRaises(ValueError):
                    open_image_safely(valid_png, max_bytes=bad_max_bytes)

            # Missing file and directory path rejected with ValueError
            with self.assertRaises(ValueError):
                open_image_safely(td_path / "missing.png")
            with self.assertRaises(ValueError):
                open_image_safely(td_path)

        # Direct magic-byte validator edge cases
        for invalid in ("not-bytes", None, 123, b"", bytearray(), b" \x00\n", b"%!PS", b"\xc5\xd0\xd3\xc6", b"%PDF"):
            with self.assertRaises(ValueError):
                validate_image_magic_bytes(invalid)  # type: ignore[arg-type]
        validate_image_magic_bytes(bytearray(b"\x89PNG\r\n\x1a\n"))

    def test_extract_zips_rejects_disguised_eps_pdf_and_oversized_images_with_cleanup(self):
        disguised_cases = [
            ("eps_ascii", "varak.jpg", b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n"),
            ("eps_ws_nul", "sub/varak.png", b" \n\x00%!PS-Adobe-3.0\nshowpage\n"),
            ("eps_binary", "varak.tif", b"\xc5\xd0\xd3\xc6" + b"\x00" * 28),
            ("pdf_magic", "varak.webp", b"  \n%PDF-1.7\n%fake\n"),
            ("empty_img", "empty.jpg", b""),
        ]
        for case_name, arcname, payload in disguised_cases:
            with tempfile.TemporaryDirectory() as td:
                in_dir = Path(td) / "in"
                out_dir = Path(td) / "out"
                in_dir.mkdir()
                zpath = in_dir / f"{case_name}.zip"
                with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as zf:
                    zf.writestr(arcname, payload)
                with self.assertRaises(ValueError, msg=f"{case_name} reddedilmeli"):
                    extract_zips_if_needed(in_dir, out_dir)
                expected_dest = out_dir / case_name / arcname
                self.assertFalse(expected_dest.exists(), f"Kısmi dosya temizlenmedi: {expected_dest}")

        # Oversized image member (> MAX_IMAGE_BYTES) rejected and cleaned up
        with tempfile.TemporaryDirectory() as td:
            in_dir = Path(td) / "in"
            out_dir = Path(td) / "out"
            in_dir.mkdir()
            png_buf = io.BytesIO()
            Image.new("RGB", (64, 64), color=(10, 20, 30)).save(png_buf, format="PNG")
            png_bytes = png_buf.getvalue()
            self.assertGreater(len(png_bytes), 64)

            zpath = in_dir / "oversized_img.zip"
            with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("big_varak.png", png_bytes)

            with patch.object(crop_and_verify, "MAX_IMAGE_BYTES", 64):
                with self.assertRaises(ValueError):
                    extract_zips_if_needed(in_dir, out_dir)
            self.assertFalse((out_dir / "oversized_img" / "big_varak.png").exists())

            # Legitimate non-image .pdf companion file inside ZIP must not trigger false positive
            zpath.unlink()
            valid_zip = in_dir / "with_notes.zip"
            with zipfile.ZipFile(valid_zip, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("notes.pdf", b"%PDF-1.7\n%legitimate metadata doc\n")
                zf.writestr("page_01.png", png_bytes)
            extracted = extract_zips_if_needed(in_dir, out_dir / "ok")
            self.assertEqual([p.name for p in extracted], ["page_01.png"])

    def test_cli_inventory_split_crop_reject_disguised_eps_pdf_cleanly(self):
        script = Path(__file__).parent / "crop_and_verify.py"
        eps_payload = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\nshowpage\n"
        pdf_payload = b"%PDF-1.7\n%disguised\n"

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            eps_jpg = td_path / "disguised_eps.jpg"
            eps_jpg.write_bytes(eps_payload)
            pdf_jpg = td_path / "disguised_pdf.jpg"
            pdf_jpg.write_bytes(pdf_payload)

            inv_dir = td_path / "inv_in"
            inv_dir.mkdir()
            (inv_dir / "varak.jpg").write_bytes(eps_payload)

            zip_dir = td_path / "zip_in"
            zip_dir.mkdir()
            with zipfile.ZipFile(zip_dir / "paket.zip", "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("varak.jpg", pdf_payload)

            commands = [
                ["inventory", "--input", str(inv_dir), "--output", str(td_path / "inv_out")],
                ["inventory", "--input", str(zip_dir), "--output", str(td_path / "zip_out")],
                ["split", "--image", str(eps_jpg), "--output", str(td_path / "split_out")],
                [
                    "crop",
                    "--image",
                    str(pdf_jpg),
                    "--box",
                    "0.1,0.1,0.5,0.5",
                    "--output",
                    str(td_path / "crop_out.png"),
                ],
            ]
            for cli_args in commands:
                proc = subprocess.run(
                    [sys.executable, "-B", str(script), *cli_args],
                    capture_output=True,
                    text=True,
                )
                combined = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, f"Beklenen red gerçekleşmedi: {cli_args}")
                self.assertIn("Hata:", combined, f"Temiz 'Hata:' mesajı yok: {combined!r}")
                self.assertNotIn("Traceback", combined, f"Ham Python traceback sızdı: {combined!r}")

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

    def test_build_lineage_mermaid_escapes_newlines_quotes_and_brackets(self):
        records = [
            {
                "hane": "1]\nX[2",
                "isim": 'Ali "Kara"\r\n[şüpheli] <tag>',
                "baba_adi": 'Mustafa [ata]\n"eski" <baba>',
                "unvan": "Ağa [1]\rReis",
                "yas": 42,
                "hicri_yil": 1293,
            }
        ]
        res = build_lineage_mermaid(records)
        mermaid_lines = res["mermaid"].splitlines()
        self.assertEqual(mermaid_lines[0], "flowchart TD")

        node_re = re.compile(r'^\s+(?:P\d+|A\d+)\["[^"\[\]<>\r\n]+"\]$')
        edge_re = re.compile(r"^\s+(?:P\d+|A\d+) --> P\d+$")
        node_lines = []
        for line in mermaid_lines[1:]:
            self.assertTrue(
                node_re.match(line) or edge_re.match(line),
                f"Mermaid sözdizimi bozuldu: {line!r}",
            )
            if node_re.match(line):
                node_lines.append(line)

        self.assertEqual(len(node_lines), 2)
        joined_nodes = "\n".join(node_lines)
        self.assertIn("&#91;şüpheli&#93;", joined_nodes)
        self.assertIn("&#91;ata&#93;", joined_nodes)
        self.assertIn("&lt;tag&gt;", joined_nodes)
        self.assertIn("&lt;baba&gt;", joined_nodes)
        self.assertNotIn("\r", res["mermaid"])

        # Markdown tablo satırları da yeni satırla bölünmemeli
        table_rows = [ln for ln in res["markdown"].splitlines() if ln.startswith("| ")]
        self.assertEqual(len(table_rows), 2)

        # migration-map diyagramında da köşeli parantez ve yeni satırlar kaçışlanmalı
        mig = trace_migration_timeline([
            {
                "hicri_yil": 1260,
                "kaza": 'Serfice "A"\n[1]',
                "karye": "Kozani <eski>",
                "olay": "Nakil\r\n[test]",
            },
            {
                "miladi_yil": 1880,
                "kaza": "Manastır",
                "karye": "İnceğiz",
                "olay": 'Göç "2" [yeni]',
            },
        ])
        for line in mig["mermaid"].splitlines()[1:]:
            self.assertNotIn("\r", line)
            self.assertNotIn("<eski>", line)
        self.assertIn("&#91;1&#93;", mig["mermaid"])
        self.assertIn("&#91;yeni&#93;", mig["mermaid"])

    def test_lazy_pil_import_text_only_subcommands_work_without_pil(self):
        script_path = Path(__file__).resolve().parent / "crop_and_verify.py"
        blocked = {"PIL": None, "PIL.Image": None}

        with patch.dict(sys.modules, blocked):
            spec = importlib.util.spec_from_file_location("crop_and_verify_no_pil", script_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            cases = [
                (["crop_and_verify.py", "calendar", "--year", "1293", "--birth-age", "35"], "1876"),
                (
                    ["crop_and_verify.py", "query-builder", "--name", "Mehmedoğlu", "--place", "Muğla", "--year", "1293"],
                    "Mehmedzade",
                ),
                (
                    [
                        "crop_and_verify.py",
                        "lineage-tree",
                        "--records-json",
                        json.dumps([{"hane": 1, "isim": "Halil", "baba_adi": "Mustafa", "yas": 40, "hicri_yil": 1260}]),
                    ],
                    "flowchart TD",
                ),
                (
                    [
                        "crop_and_verify.py",
                        "migration-map",
                        "--records-json",
                        json.dumps([{"hicri_yil": 1260, "kaza": "Üsküdar", "karye": "Yakacık"}]),
                    ],
                    "flowchart LR",
                ),
                (
                    [
                        "crop_and_verify.py",
                        "verify-citations",
                        "--records-json",
                        json.dumps([{"isim": "Halil", "fon": "BOA, NFS.d. 1423", "vr": "12a"}]),
                    ],
                    '"compliant": true',
                ),
            ]

            for argv, expected in cases:
                buf = io.StringIO()
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
                    mod.main()
                self.assertIn(expected, buf.getvalue())

            # Görüntü komutu / yardımcısı Pillow olmadığında temiz hata mesajıyla çıkar
            with self.assertRaises(SystemExit) as ctx:
                mod._require_pil_image()
            self.assertIn("Pillow", str(ctx.exception))

            # Saf stdlib magic-byte reddi Pillow kurulu olmadan da çalışır
            for bad_bytes in (
                b"%!PS-Adobe-3.0 EPSF-3.0\n",
                b" \n\x00%!PS-Adobe-3.0\n",
                b"\xc5\xd0\xd3\xc6\x00\x00\x00\x00",
                b"%PDF-1.7\n",
                b"",
            ):
                with self.assertRaises(ValueError):
                    mod.validate_image_magic_bytes(bad_bytes)

            with tempfile.TemporaryDirectory() as td:
                eps_file = Path(td) / "disguised.jpg"
                eps_file.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n")
                with self.assertRaises(ValueError):
                    mod.open_image_safely(eps_file)


class TestMermaidInjectionHardening(unittest.TestCase):
    def test_sanitize_mermaid_label_blocks_xss_quotes_directives_and_unicode_breaks(self):
        payload = (
            'Ali"]; click P1 href "javascript:alert(1)" _blank\r\n'
            "%%{init: {'securityLevel': 'loose'}}%%\u2028"
            "<img src=x onerror='alert(1)'> #quot; `code` | {x} [y] \\ vbscript:x data:text/html"
        )
        cleaned = sanitize_mermaid_label(payload)
        self.assertEqual(_escape_mermaid_label(payload, escape_pipe=True), cleaned)
        for forbidden in (
            "\n",
            "\r",
            "\u2028",
            "\u2029",
            '"',
            "'",
            "<",
            ">",
            "`",
            "|",
            "[",
            "]",
            "{",
            "}",
            "\\",
            "%%",
            "#quot;",
            "javascript:",
            "vbscript:",
            "data:",
        ):
            self.assertNotIn(forbidden, cleaned, f"Forbidden token leaked: {forbidden!r}")
        self.assertNotRegex(cleaned, r"(?<!&)#|&#(?!91;|93;)")
        self.assertNotRegex(cleaned, r"(?i)\b(?:click|href|classdef|linkstyle|callback)\b")
        self.assertIn("&lt;img", cleaned)
        self.assertIn("&quot;", cleaned)
        self.assertIn("&apos;", cleaned)
        self.assertIn("&#91;y&#93;", cleaned)

    def test_build_lineage_mermaid_injection_hardening(self):
        res = build_lineage_mermaid([
            {
                "hane": '14"];\nclick P1 "javascript:alert(1)"',
                "isim": 'Halil<script>alert(1)</script> %%{init:{"securityLevel":"loose"}}%%',
                "baba_adi": "Mustafa'\u2028click A1 href 'https://evil.test' #quot;",
                "unvan": 'Ağa | `fence` [x]{y}',
                "yas": 52,
                "hicri_yil": 1260,
            }
        ])
        mermaid = res["mermaid"]
        lines = mermaid.splitlines()
        self.assertEqual(lines[0], "flowchart TD")
        for line in lines[1:]:
            self.assertRegex(
                line,
                r'^    (?:[PA]\d+\["[^"]*"\]|[PA]\d+ --> P\d+)$',
                f"Unexpected Mermaid syntax line: {line}",
            )
            # Quoted node lines must contain exactly two literal double-quotes
            if '["' in line:
                self.assertEqual(line.count('"'), 2, f"Unescaped quote breakout in: {line}")

        self.assertNotIn("%%", mermaid)
        self.assertNotIn("javascript:", mermaid.lower())
        self.assertNotIn("<script>", mermaid)
        self.assertNotRegex(mermaid, r"(?i)\b(?:click|href)\b")
        self.assertIn("&lt;script&gt;", mermaid)
        self.assertIn("&quot;", mermaid)

        # Markdown table must also escape raw HTML and pipes
        md_table_part = res["markdown"].split("```mermaid")[0]
        self.assertNotIn("<script>", md_table_part)
        self.assertIn("&lt;script&gt;", md_table_part)
        for row in [ln for ln in md_table_part.splitlines() if ln.startswith("| ")]:
            # Header + data rows always have 6 columns -> exactly 7 pipes
            self.assertEqual(row.count("|"), 7, f"Markdown table column breakout in: {row}")

    def test_trace_migration_timeline_injection_hardening(self):
        records = [
            {
                "hicri_yil": 1260,
                "kaza": 'Serfice"];\nclick L1 "javascript:alert(1)"',
                "nahiye": "Velvendos%%{init:{'theme':'dark'}}%%",
                "karye": "Kozani<img src=x onerror=alert(1)>",
                "eski_ad": "Kozana `| [evil] #34;",
                "kisi": "Halil <svg onload=alert(1)>",
                "olay": 'Temettuat"| L2; click L1 href "https://evil.test" ; L1 -->|"',
                "fon": "ML.VRD.TMT.d",
            },
            {
                "miladi_yil": 1924,
                "kaza": "Çatalca",
                "nahiye": "Merkez",
                "karye": "İnceğiz",
                "olay": 'Mübadele\u2029click L2 "javascript:void(0)"',
                "fon": "BCA 130.16.13.2",
            },
        ]
        res = trace_migration_timeline(records)
        mermaid = res["mermaid"]
        lines = mermaid.splitlines()
        self.assertEqual(lines[0], "flowchart LR")
        for line in lines[1:]:
            self.assertRegex(
                line,
                r'^    (?:L\d+\["[^"]*"\]|L\d+ -->\|"[^"|]*"\| L\d+)$',
                f"Unexpected Mermaid migration line: {line}",
            )
            self.assertEqual(line.count('"'), 2, f"Unescaped quote breakout in: {line}")

        self.assertNotIn("%%", mermaid)
        self.assertNotIn("<img", mermaid)
        self.assertNotIn("<svg", mermaid)
        self.assertNotIn("javascript:", mermaid.lower())
        self.assertNotRegex(mermaid, r"(?i)\b(?:click|href)\b")
        self.assertIn("&lt;img", mermaid)

        # Markdown table & cross-reference bullets must escape HTML/pipes
        md_before_fence = res["markdown"].split("```mermaid")[0]
        self.assertNotIn("<img", md_before_fence)
        self.assertNotIn("<svg", md_before_fence)
        self.assertIn("&lt;img", md_before_fence)
        self.assertIn("&lt;svg", md_before_fence)

    def test_sanitize_markdown_cell(self):
        cleaned = sanitize_markdown_cell("Ali | <script>x</script>\n`tick`")
        self.assertEqual(cleaned, "Ali / &lt;script&gt;x&lt;/script&gt; 'tick'")


class TestVerifyCitations(unittest.TestCase):
    def test_verify_citations_dataset_compliant_mixed_sources_and_unverified(self):
        records = [
            {
                "hane": 14,
                "isim": "Halil",
                "baba_adi": "Mustafa",
                "fon": "BOA, NFS.d. 1423",
                "vr": "12a",
                "poz": "18",
            },
            {
                "isim": "Mehmed",
                "karye": "İnceğiz",
                "fon": "BCA 130.16.13.2",
                "kutu": "4",
                "dosya": "12",
                "gomlek": "3",
            },
            {
                "isim": "Ahmet",
                "edevlet_ref": "e-Devlet Alt-Üst Soy Belgesi (Belge No: 9912)",
            },
            {
                "isim": "Hasan",
                "kaynak": "NVİGM I\u0307sim Denklik Belgesi",
            },
            {
                "isim": "Hüseyin",
                "durum": "DOĞRULANMADI",
                "not": "Ara nüfus defteri henüz incelenmedi",
            },
            {
                "isim": "Veli",
                "fon": "BOA, NFS.d. 1423",
                "verified": False,
                "not": "Aday kayıt; filigran nedeniyle henüz doğrulanmadı",
            },
            {
                "isim": "Kamil",
                "source": "Fond 42, Box 7, Folder 3",
            },
        ]
        report = verify_citations(records)
        self.assertTrue(report["compliant"])
        self.assertEqual(report["input_format"], "dataset")
        self.assertEqual(report["summary"]["total_claims"], 7)
        self.assertEqual(report["summary"]["cited"], 5)
        self.assertEqual(report["summary"]["explicitly_unverified"], 2)
        self.assertEqual(report["summary"]["uncited"], 0)
        self.assertEqual(report["summary"]["coverage_ratio"], 1.0)
        self.assertEqual(report["uncited_claims"], [])
        self.assertEqual(report["claims"][5]["status"], "EXPLICITLY_UNVERIFIED")

    def test_verify_citations_dataset_rejects_uncited_and_placeholder_hallucinations(self):
        records = [
            {"hane": 1, "isim": "Ali", "baba_adi": "Osman", "yas": 40, "hicri_yil": 1260},
            {"isim": "Bekir", "kaynak": "aile rivayeti"},
            {"isim": "Cemal", "fon": "bilinmiyor"},
            {"isim": "Dursun", "source": "tahmin"},
            {"isim": "Emin", "fon": "ML.VRD.TMT.d. 4521", "vr": "8a"},
        ]
        report = verify_citations({"records": records})
        self.assertFalse(report["compliant"])
        self.assertEqual(report["summary"]["total_claims"], 5)
        self.assertEqual(report["summary"]["cited"], 1)
        self.assertEqual(report["summary"]["uncited"], 4)
        self.assertEqual(len(report["uncited_claims"]), 4)
        self.assertEqual([c["index"] for c in report["uncited_claims"]], [1, 2, 3, 4])

    def test_verify_citations_require_locator_flag(self):
        bare_fond = [{"isim": "Halil", "fon": "NFS.d"}]
        default_rep = verify_citations(bare_fond)
        self.assertTrue(default_rep["compliant"])
        self.assertTrue(len(default_rep["warnings"]) == 1)

        strict_rep = verify_citations(bare_fond, require_locator=True)
        self.assertFalse(strict_rep["compliant"])
        self.assertEqual(strict_rep["summary"]["uncited"], 1)

    def test_verify_citations_markdown_compliant_with_footnotes_and_code_blocks(self):
        md = """# Baba Tarafı Soy Kütüğü Raporu

| Kuşak | İsim | Baba Adı | Doğum | Arşiv Künyesi / Kaynak |
|-------|------|----------|-------|------------------------|
| 1 | Halil | Mustafa | ~1792 | BOA, ML.VRD.TMT.d. 4521, vr. 8a, Mi=12 |
| 2 | Mehmed | Halil | ~1820 | e-Devlet Alt-Üst Soy Belgesi |
| 3 | Hüseyin | Mehmed | ~1850 | doğrulanmadı |

- **İsmail bin Halil** (1877 mübadili) [^mub]
- **Osman** (1835 köy muhtarı): BOA, A.{RSK.d. 142, vr. 4b

```mermaid
flowchart TD
    P1["Atıf içermeyen diyagram bloğu atlanmalı"]
```

[^mub]: BCA 130-16-13-2, Kutu 12, Dosya 4, Gömlek 9
"""
        report = verify_citations(md)
        self.assertTrue(report["compliant"], f"Unexpected uncited: {report['uncited_claims']}")
        self.assertEqual(report["input_format"], "markdown")
        self.assertEqual(report["summary"]["total_claims"], 5)
        self.assertEqual(report["summary"]["cited"], 4)
        self.assertEqual(report["summary"]["explicitly_unverified"], 1)
        self.assertEqual(report["summary"]["uncited"], 0)

        rendered_md = format_citation_report_markdown(report)
        self.assertIn("UYUMLU (0 atıfsız iddia)", rendered_md)
        self.assertIn("`CITED`", rendered_md)
        self.assertIn("`EXPLICITLY_UNVERIFIED`", rendered_md)

    def test_verify_citations_markdown_rejects_hallucinated_and_uncited_claims(self):
        md = """# Eksik Atıflı Rapor

| Kuşak | İsim | Kaynak |
|-------|------|--------|
| 1 | Halil bin Mustafa | BOA, NFS.d. 1423, vr. 12a |
| 2 | Uydurma Ata | — |

- **Süleyman bin Ali** [KESİN KANIT] — 1780 yılında Selanik'ten geldi.

Büyük dedemiz Veli bin Hasan 1844 yılında Yakacık köyünde yaşamıştır.
"""
        report = verify_citations(md)
        self.assertFalse(report["compliant"])
        self.assertEqual(report["summary"]["total_claims"], 4)
        self.assertEqual(report["summary"]["cited"], 1)
        self.assertEqual(report["summary"]["uncited"], 3)

    def test_cli_verify_citations_exit_codes_and_formats(self):
        script = Path(__file__).parent / "crop_and_verify.py"

        # 1) Clean JSON dataset -> exit 0 + valid JSON output
        clean_payload = json.dumps([
            {"isim": "Halil", "fon": "BOA, NFS.d. 1423", "vr": "12a", "poz": "18"},
            {"isim": "Ali", "durum": "doğrulanmadı"},
        ])
        proc_ok = subprocess.run(
            [sys.executable, str(script), "verify-citations", "--records-json", clean_payload],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
        parsed_ok = json.loads(proc_ok.stdout)
        self.assertTrue(parsed_ok["compliant"])
        self.assertEqual(parsed_ok["summary"]["uncited"], 0)

        # 2) Uncited JSON dataset -> exit 1 + structured JSON output still emitted
        bad_payload = json.dumps([
            {"isim": "Halil", "fon": "BOA, NFS.d. 1423", "vr": "12a"},
            {"isim": "Hayali Dede", "baba_adi": "Bilinmez"},
        ])
        proc_bad = subprocess.run(
            [sys.executable, str(script), "verify-citations", "--records-json", bad_payload],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc_bad.returncode, 1)
        parsed_bad = json.loads(proc_bad.stdout)
        self.assertFalse(parsed_bad["compliant"])
        self.assertEqual(parsed_bad["summary"]["uncited"], 1)

        # 3) Markdown file input with --format markdown -> exit 0 on clean, exit 1 on uncited
        with tempfile.TemporaryDirectory() as td:
            clean_md = Path(td) / "clean.md"
            clean_md.write_text(
                "- **Halil bin Mustafa** (1844): BOA, ML.VRD.TMT.d. 4521, vr. 8a\n"
                "- **Veli bin Halil** (~1815): doğrulanmadı\n",
                encoding="utf-8",
            )
            proc_md_ok = subprocess.run(
                [sys.executable, str(script), "verify-citations", "--input", str(clean_md), "--format", "markdown"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc_md_ok.returncode, 0, proc_md_ok.stderr)
            self.assertIn("UYUMLU", proc_md_ok.stdout)

            bad_md = Path(td) / "bad.md"
            bad_md.write_text(
                "- **Halil bin Mustafa** 1844 yılında köyde doğdu.\n",
                encoding="utf-8",
            )
            proc_md_bad = subprocess.run(
                [sys.executable, str(script), "verify-citations", "--markdown", str(bad_md), "--format", "markdown"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc_md_bad.returncode, 1)
            self.assertIn("UYUMSUZ", proc_md_bad.stdout)
            self.assertIn("`UNCITED`", proc_md_bad.stdout)

            # 4) Path traversal blocked
            proc_trav = subprocess.run(
                [sys.executable, str(script), "verify-citations", "--input", str(Path(td) / ".." / "escape.md")],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc_trav.returncode, 0)


if __name__ == "__main__":
    unittest.main()
