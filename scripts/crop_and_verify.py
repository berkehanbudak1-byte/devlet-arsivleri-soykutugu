#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Devlet Arşivleri Soy Kütüğü Becerisi — 1:1 Piksel Kesit ve Varak Ayırma Aracı
=============================================================================
Kullanıcının e-Satış sepetinden indirip klasöre koyduğu .zip veya .jpg/.png
uzantılı Osmanlıca defter görüntülerini:
  1) Otomatik olarak açar ve envanterini çıkarır (`inventory`),
  2) Çift sayfa (varak) taramaları Sağ Varak (`_R`) ve Sol Varak (`_L`) ile
     okunaklı yatay şeritlere (`_top`, `_mid`, `_bot`) ayırır (`split`),
  3) Kritik hane, isim, yaş veya şerh satırlarından 1:1 doğal piksel (veya 2x/3x
     piksel yakınlaştırmalı) kanıt kesiti (`crop`) üretir.

Gereksinim: Python 3.8+ ve Pillow (`pip install Pillow`)
"""

import argparse
import json
import math
import os
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath

try:
    from PIL import Image
except ImportError:
    sys.exit("Hata: 'Pillow' kütüphanesi kurulu değil. Kurmak için: pip install Pillow")

# Güvenlik sınırları (decompression bomb / zip bomb / çıktı boyut koruması)
MAX_IMAGE_PIXELS = 100_000_000  # 100 MP üst sınır
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

MAX_ZIP_FILES = 2000
MAX_ZIP_TOTAL_BYTES = 500 * 1024 * 1024  # 500 MB toplam açılmış boyut sınırı
MAX_ZIP_FILE_BYTES = 100 * 1024 * 1024   # 100 MB tekil dosya sınırı
MAX_ZIP_COMPRESSION_RATIO = 100.0
MAX_JSON_BYTES = 5 * 1024 * 1024         # 5 MB JSON girdi sınırı

MIN_CROP_ZOOM = 0.1
MAX_CROP_ZOOM = 8.0
MAX_OUTPUT_DIMENSION = 12000
MAX_OUTPUT_PIXELS = 64_000_000

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')


def sanitize_filename(name: str, fallback: str = "output", max_length: int = 120) -> str:
    """Dosya adından yol ayırıcı, kontrol karakteri ve geçiş (..) dizilerini temizler."""
    raw = str(name or "").strip()
    raw = _UNSAFE_FILENAME_RE.sub("_", raw)
    raw = raw.replace("..", "_")
    raw = re.sub(r"\s+", "_", raw).strip("._ ")
    if not raw:
        return fallback
    return raw[:max_length]


def validate_safe_path(
    raw_path: str | Path,
    base_dir: Path | None = None,
    *,
    allow_symlinks: bool = False,
) -> Path:
    """
    Yol enjeksiyonu (NUL/kontrol karakterleri, '..' traversal segmentleri ve
    isteğe bağlı taban klasör dışına çıkış) kontrollerini uygular.
    """
    path_str = str(raw_path or "")
    if not path_str.strip():
        raise ValueError("Dosya/klasör yolu boş olamaz.")
    if _CONTROL_CHAR_RE.search(path_str):
        raise ValueError("Dosya/klasör yolunda geçersiz kontrol karakteri tespit edildi.")

    p = Path(path_str)
    if ".." in p.parts:
        raise ValueError(f"Güvensiz yol geçişi ('..') reddedildi: {path_str}")
    if not allow_symlinks and p.is_symlink():
        raise ValueError(f"Sembolik bağlantı (symlink) yolu reddedildi: {path_str}")

    resolved = p.resolve()
    if base_dir is not None:
        base_resolved = base_dir.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(f"Hedef yol izin verilen dizin dışında: {path_str}")
    return resolved


def open_image_safely(img_path: Path) -> Image.Image:
    """Görüntüyü decompression bomb koruması (Image.MAX_IMAGE_PIXELS) etkin biçimde açar."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        im = Image.open(img_path)
    except Image.DecompressionBombError as exc:
        raise ValueError(
            f"Görüntü piksel üst sınırını ({MAX_IMAGE_PIXELS} px) aşıyor (decompression bomb koruması): {exc}"
        ) from exc
    if im.width * im.height > MAX_IMAGE_PIXELS:
        pixels = im.width * im.height
        im.close()
        raise ValueError(
            f"Görüntü piksel üst sınırını ({pixels} > {MAX_IMAGE_PIXELS} px) aşıyor (decompression bomb koruması)."
        )
    return im


def compute_safe_crop_box(
    width: int,
    height: int,
    box_str: str,
    zoom: float = 1.0,
) -> tuple[int, int, int, int, int, int]:
    """
    Kesit koordinatlarını ve büyütme katsayısını doğrular; çıktı piksel ve kenar
    boyutlarını güvenli üst sınırlar içinde tutar.
    Dönüş: (x1, y1, x2, y2, out_w, out_h)
    """
    if width <= 0 or height <= 0:
        raise ValueError("Kaynak görüntü boyutları pozitif olmalıdır.")
    if not isinstance(zoom, (int, float)) or not math.isfinite(zoom):
        raise ValueError("--zoom parametresi geçerli bir sayı olmalıdır.")
    if zoom < MIN_CROP_ZOOM or zoom > MAX_CROP_ZOOM:
        raise ValueError(
            f"--zoom parametresi {MIN_CROP_ZOOM} ile {MAX_CROP_ZOOM} arasında olmalıdır."
        )

    try:
        vals = [float(v.strip()) for v in str(box_str).split(",")]
    except ValueError as exc:
        raise ValueError("--box parametresi sayısal değerlerden oluşmalıdır ('x1,y1,x2,y2').") from exc
    if len(vals) != 4 or not all(math.isfinite(v) for v in vals):
        raise ValueError(
            "--box parametresi 'x1,y1,x2,y2' formatında 4 sonlu sayı olmalıdır (0.0-1.0 oran veya piksel)."
        )
    if any(v < 0 for v in vals):
        raise ValueError("--box koordinatları negatif olamaz.")

    if all(0.0 <= v <= 1.0 for v in vals):
        x1, y1, x2, y2 = int(vals[0] * width), int(vals[1] * height), int(vals[2] * width), int(vals[3] * height)
    else:
        x1, y1, x2, y2 = [int(v) for v in vals]

    x1, x2 = max(0, min(width, x1)), max(0, min(width, x2))
    y1, y2 = max(0, min(height, y1)), max(0, min(height, y2))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Geçersiz kesit koordinatları ({x1},{y1},{x2},{y2}); x2 > x1 ve y2 > y1 olmalıdır.")

    crop_w = x2 - x1
    crop_h = y2 - y1
    out_w = max(1, int(round(crop_w * zoom))) if zoom != 1.0 else crop_w
    out_h = max(1, int(round(crop_h * zoom))) if zoom != 1.0 else crop_h

    if out_w > MAX_OUTPUT_DIMENSION or out_h > MAX_OUTPUT_DIMENSION:
        raise ValueError(
            f"Kesit çıktı boyutu ({out_w}x{out_h}) izin verilen tek kenar sınırını ({MAX_OUTPUT_DIMENSION} px) aşıyor."
        )
    if out_w * out_h > MAX_OUTPUT_PIXELS:
        raise ValueError(
            f"Kesit toplam piksel sayısı ({out_w * out_h}) izin verilen sınırı ({MAX_OUTPUT_PIXELS} px) aşıyor."
        )
    return x1, y1, x2, y2, out_w, out_h


def _is_zip_symlink(member: zipfile.ZipInfo) -> bool:
    """ZIP üyesinin Unix sembolik bağlantısı olup olmadığını kontrol eder."""
    unix_mode = (member.external_attr >> 16) & 0o170000
    return unix_mode == 0o120000


def extract_zips_if_needed(input_dir: Path, extract_root: Path) -> list[Path]:
    """Klasördeki .zip arşivlerini güvenli biçimde açar ve tüm görüntü yollarını döndürür."""
    input_resolved = validate_safe_path(input_dir)
    extract_root_resolved = validate_safe_path(extract_root)

    images: list[Path] = []
    for root, _, files in os.walk(input_resolved, followlinks=False):
        for fn in sorted(files):
            fpath = Path(root) / fn
            if fpath.is_symlink():
                continue
            ext = fpath.suffix.lower()
            if ext == ".zip":
                safe_stem = sanitize_filename(fpath.stem, fallback="archive")
                target = validate_safe_path(extract_root_resolved / safe_stem, base_dir=extract_root_resolved)
                if not target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(fpath, "r") as zf:
                        infos = zf.infolist()
                        if len(infos) > MAX_ZIP_FILES:
                            raise ValueError(
                                f"ZIP arşivi izin verilen dosya sayısını ({MAX_ZIP_FILES}) aşıyor: {fpath.name}"
                            )
                        total_uncompressed = 0
                        actual_total_written = 0
                        target_resolved = target.resolve()
                        for member in infos:
                            raw_member_name = member.filename
                            if _CONTROL_CHAR_RE.search(raw_member_name):
                                raise ValueError(f"Güvensiz zip üyesi adı reddedildi: {raw_member_name!r}")
                            if _is_zip_symlink(member):
                                raise ValueError(f"ZIP içindeki sembolik bağlantı reddedildi: {raw_member_name}")
                            normalized_posix = raw_member_name.replace("\\", "/")
                            posix_parts = PurePosixPath(normalized_posix).parts
                            if (
                                normalized_posix.startswith("/")
                                or ".." in posix_parts
                                or (len(normalized_posix) >= 2 and normalized_posix[1] == ":")
                            ):
                                raise ValueError(f"Güvensiz zip yolu reddedildi: {raw_member_name}")

                            dest = (target_resolved / normalized_posix).resolve()
                            if not dest.is_relative_to(target_resolved):
                                raise ValueError(f"Güvensiz zip yolu reddedildi: {raw_member_name}")

                            if member.file_size > MAX_ZIP_FILE_BYTES:
                                raise ValueError(
                                    f"ZIP üyesi boyut sınırını ({MAX_ZIP_FILE_BYTES} B) aşıyor: {raw_member_name}"
                                )
                            total_uncompressed += member.file_size
                            if total_uncompressed > MAX_ZIP_TOTAL_BYTES:
                                raise ValueError(
                                    f"ZIP toplam açılmış boyut sınırını ({MAX_ZIP_TOTAL_BYTES} B) aşıyor: {fpath.name}"
                                )
                            if member.compress_size > 0 and (member.file_size / member.compress_size) > MAX_ZIP_COMPRESSION_RATIO:
                                raise ValueError(
                                    f"Şüpheli sıkıştırma oranı (zip bomb koruması) reddedildi: {raw_member_name}"
                                )

                            if member.is_dir():
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    with zf.open(member, "r") as src_fh, open(dest, "wb") as dst_fh:
                                        written = 0
                                        while True:
                                            chunk = src_fh.read(65536)
                                            if not chunk:
                                                break
                                            written += len(chunk)
                                            actual_total_written += len(chunk)
                                            if written > MAX_ZIP_FILE_BYTES:
                                                raise ValueError(
                                                    f"ZIP üyesi gerçek boyut sınırını aşıyor: {raw_member_name}"
                                                )
                                            if actual_total_written > MAX_ZIP_TOTAL_BYTES:
                                                raise ValueError(
                                                    f"ZIP toplam gerçek açılmış boyut sınırını ({MAX_ZIP_TOTAL_BYTES} B) aşıyor: {fpath.name}"
                                                )
                                            dst_fh.write(chunk)
                                except Exception:
                                    dest.unlink(missing_ok=True)
                                    raise
                for zroot, _, zfiles in os.walk(target, followlinks=False):
                    for zfn in sorted(zfiles):
                        zp = Path(zroot) / zfn
                        if zp.is_symlink():
                            continue
                        if zp.suffix.lower() in IMAGE_EXTS:
                            images.append(zp)
            elif ext in IMAGE_EXTS:
                images.append(fpath)

    # Tekrarları koruyarak benzersizleştir
    seen = set()
    unique_imgs = []
    for p in images:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique_imgs.append(p)
    return unique_imgs


def cmd_inventory(args: argparse.Namespace) -> None:
    try:
        in_dir = validate_safe_path(args.input)
        out_dir = validate_safe_path(args.output)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")
    if not in_dir.exists():
        sys.exit(f"Hata: Girdi klasörü bulunamadı: {in_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        imgs = extract_zips_if_needed(in_dir, out_dir / "_extracted")
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")
    print(f"Toplam Görüntü Sayısı: {len(imgs)}")
    for idx, p in enumerate(imgs, 1):
        try:
            with open_image_safely(p) as im:
                w, h = im.size
        except ValueError as exc:
            sys.exit(f"Hata: {exc}")
        print(f"[{idx:03d}] {p.name}  ({w}x{h} px)  ->  {p}")


def cmd_split(args: argparse.Namespace) -> None:
    try:
        img_path = validate_safe_path(args.image)
        out_dir = validate_safe_path(args.output)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")
    if not img_path.is_file():
        sys.exit(f"Hata: Görüntü dosyası bulunamadı: {img_path}")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open_image_safely(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            stem = sanitize_filename(img_path.stem, fallback="varak")

            # Osmanlı defterlerinde sağ sayfa önce (a yüzü), sol sayfa sonra (b yüzü) okunur.
            halves = [
                ("R", im.crop((int(w * 0.47), 0, w, h))),
                ("L", im.crop((0, 0, int(w * 0.53), h))),
            ]
            for side, sub in halves:
                sw, sh = sub.size
                full_side = validate_safe_path(out_dir / f"{stem}_{side}_full.jpg", base_dir=out_dir)
                sub.save(full_side, quality=92)
                print(f"Kaydedildi: {full_side} ({sw}x{sh})")

                if args.bands:
                    bands = [
                        ("1_top", (0, 0, sw, int(sh * 0.37))),
                        ("2_mid", (0, int(sh * 0.31), sw, int(sh * 0.69))),
                        ("3_bot", (0, int(sh * 0.63), sw, sh)),
                    ]
                    for bname, box in bands:
                        bp = validate_safe_path(out_dir / f"{stem}_{side}_{bname}.png", base_dir=out_dir)
                        sub.crop(box).save(bp)
                        print(f"  Şerit: {bp}")
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")


def cmd_crop(args: argparse.Namespace) -> None:
    try:
        img_path = validate_safe_path(args.image)
        out_path = validate_safe_path(args.output)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")
    if not img_path.is_file():
        sys.exit(f"Hata: Görüntü dosyası bulunamadı: {img_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open_image_safely(img_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            zoom_val = float(args.zoom) if args.zoom is not None else 1.0
            x1, y1, x2, y2, out_w, out_h = compute_safe_crop_box(w, h, args.box, zoom=zoom_val)

            crop = im.crop((x1, y1, x2, y2))
            if zoom_val != 1.0:
                resample = Image.Resampling.NEAREST if args.nearest else Image.Resampling.LANCZOS
                crop = crop.resize((out_w, out_h), resample)

            crop.save(out_path)
            print(f"1:1 Kanıt Kesiti Kaydedildi: {out_path} ({crop.width}x{crop.height} px, kaynak kutu=({x1},{y1},{x2},{y2}))")
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")


def hicri_miladi_cevir(year: int) -> dict[str, int | str]:
    """
    Osmanlı Nüfus, Temettuat ve Tapu tahrir defterlerindeki Hicrî (Kamerî) ve
    Rumî (Malî) yılları Miladî (Gregoryen) yıla çevirir.
    - Hicrî -> Miladî: M ≈ H - floor(H / 33) + 622 (veya H * 0.970224 + 621.57)
    - Rumî  -> Miladî: M = R + 584 (1 Mart başlangıçlı malî yıl; Ocak-Şubat +585)
    """
    if year <= 0:
        raise ValueError("Yıl pozitif bir tam sayı olmalıdır.")
    hicri_to_miladi = int(round(year * 0.970224 + 621.57))
    rumi_to_miladi = year + 584
    if year < 1500:
        donem_notu = (
            f"1246-1261 (1830-1845) İlk Nüfus/Temettuat dönemi"
            if 1240 <= year <= 1265
            else "1293-1324 (1877-1908) Sicill-i Nüfus / İkinci Tahrir dönemi"
            if 1280 <= year <= 1335
            else "Osmanlı Arşiv Dönemi"
        )
    else:
        donem_notu = "Miladî yıl girdisi (geriye dönüşüm için Hicrî ≈ M - 622 + floor((M - 622)/32))"
    return {
        "girdi_yili": year,
        "hicri_ise_miladi": hicri_to_miladi,
        "hicri_aralik": f"{hicri_to_miladi - 1}/{hicri_to_miladi}",
        "rumi_ise_miladi": rumi_to_miladi,
        "rumi_mart_subat_aralik": f"{rumi_to_miladi}/{rumi_to_miladi + 1}",
        "donem_notu": donem_notu,
    }


def cmd_calendar(args: argparse.Namespace) -> None:
    try:
        info = hicri_miladi_cevir(args.year)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")
    if getattr(args, "birth_age", None) is not None:
        age = int(args.birth_age)
        b_hicri = info["hicri_ise_miladi"] - age
        b_rumi = info["rumi_ise_miladi"] - age
        info["tahmini_dogum_miladi_hicriye_gore"] = b_hicri
        info["tahmini_dogum_miladi_rumiye_gore"] = b_rumi
    print(f"=== Osmanlı Arşiv Takvim Dönüşümü (Yıl: {info['girdi_yili']}) ===")
    print(f"  Hicrî {info['girdi_yili']}  -> Miladî {info['hicri_ise_miladi']} ({info['hicri_aralik']})")
    print(f"  Rumî  {info['girdi_yili']}  -> Miladî {info['rumi_ise_miladi']} (Mart-Şubat: {info['rumi_mart_subat_aralik']})")
    print(f"  Dönem Notu : {info['donem_notu']}")
    if "tahmini_dogum_miladi_hicriye_gore" in info:
        print(
            f"  Yaş ({args.birth_age}) -> Tahmini Doğum: "
            f"Hicrî deftere göre ~{info['tahmini_dogum_miladi_hicriye_gore']}, "
            f"Rumî deftere göre ~{info['tahmini_dogum_miladi_rumiye_gore']}"
        )


def _generate_spelling_variants(term: str) -> list[str]:
    """
    Osmanlıca -> Latin harf aktarımında (transkripsiyon) arşiv kataloglarında sık
    görülen ses/harf nöbetleşmelerini (ğ/g/k, ç/c, ş/s, ü/u, ö/o, ı/i, b/p, d/t)
    ve sülale/nispet eklerini (-oğlu / -zade / veled-i) üretir.
    """
    cleaned = " ".join(term.strip().split())
    if not cleaned:
        return []

    variants: list[str] = []

    def add_v(val: str) -> None:
        v = val.strip()
        if v and v not in variants:
            variants.append(v)

    add_v(cleaned)

    lower_map = str.maketrans("ÇĞİÖŞÜ", "çğıöşü")
    low = cleaned.translate(lower_map).lower()

    # ASCII / katalog sadeleştirmesi (ş->s, ç->c, ğ->g, ü->u, ö->o, ı->i)
    ascii_table = str.maketrans({
        "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
        "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U",
    })
    add_v(cleaned.translate(ascii_table))

    # ğ -> k / g dönüşümü (örn. Muğla -> Mukla / Mugla, Beyoğlu -> Beyoklu)
    if "ğ" in low or "g" in low:
        add_v(cleaned.replace("ğ", "g").replace("Ğ", "G"))
        add_v(cleaned.replace("ğ", "k").replace("Ğ", "K"))
    if "k" in low:
        add_v(cleaned.replace("k", "ğ").replace("K", "Ğ"))
        add_v(cleaned.replace("k", "g").replace("K", "G"))

    # ç <-> c, ş <-> s, ü <-> u, ö <-> o, d <-> t, b <-> p
    for src, dst in [("ç", "c"), ("c", "ç"), ("ş", "s"), ("s", "ş"), ("ü", "u"), ("ö", "o"), ("d", "t"), ("b", "p")]:
        if src in low:
            idx = low.find(src)
            mutated = cleaned[:idx] + (dst.upper() if cleaned[idx].isupper() else dst) + cleaned[idx + len(src):]
            add_v(mutated)

    # Sülale / lakap ekleri: -oğlu <-> -zade <-> veled-i / bin
    for suf in ("oğlu", "oğulları", "oglu", "zade", "zâde"):
        if low.endswith(suf) and len(cleaned) > len(suf) + 1:
            root = cleaned[: len(cleaned) - len(suf)].rstrip(" -'")
            add_v(f"{root}oğlu")
            add_v(f"{root}oglu")
            add_v(f"{root}zade")
            add_v(f"{root}zâde")
            add_v(f"veled-i {root}")
            add_v(f"bin {root}")
            break
    else:
        # Tekil şahıs veya lakap ise -oğlu / -zade / veled-i kombinasyonlarını da ekle
        first_token = cleaned.split()[0]
        add_v(f"{first_token}oğlu")
        add_v(f"{first_token}zade")
        add_v(f"veled-i {first_token}")

    return variants


def build_archive_queries(
    name: str = "",
    place: str = "",
    year: int | None = None,
) -> dict[str, object]:
    """
    DAB (BOA) Katalog Tarama sistemi için şahıs/lakap, yer adı (karye/kaza/sancak)
    ve Hicrî/Rumî-Miladî tarih aralığı kombinasyonlarından oluşan arama sorguları üretir.
    """
    name_str = (name or "").strip()
    place_str = (place or "").strip()
    if not name_str and not place_str:
        raise ValueError("En az '--name' veya '--place' parametrelerinden biri girilmelidir.")

    name_variants = _generate_spelling_variants(name_str) if name_str else []
    place_variants = _generate_spelling_variants(place_str) if place_str else []

    calendar_info = None
    date_window = None
    if year is not None:
        calendar_info = hicri_miladi_cevir(int(year))
        miladi_h = int(calendar_info["hicri_ise_miladi"])
        miladi_r = int(calendar_info["rumi_ise_miladi"])
        min_y = min(miladi_h, miladi_r) - 5
        max_y = max(miladi_h, miladi_r) + 5
        date_window = {
            "girdi_yili": int(year),
            "hicri_miladi": miladi_h,
            "rumi_miladi": miladi_r,
            "onerilen_miladi_aralik": f"{min_y}-{max_y}",
            "onerilen_hicri_aralik": f"{max(1, int(year) - 5)}-{int(year) + 5}",
        }

    recommended_fonlar = [
        "NFS.d (BOA Nüfus Defterleri)",
        "ML.VRD.TMT.d (Maliye Varidat Temettuat Defterleri)",
        "TT.d (Tapu Tahrir Defterleri)",
        "ŞD (Şura-yı Devlet) / DH.MKT (Dahiliye Mektubi Kalemi)",
    ]

    queries: list[str] = []
    if place_variants and name_variants:
        for pv in place_variants[:4]:
            for nv in name_variants[:4]:
                q = f'"{pv}" "{nv}"'
                if q not in queries:
                    queries.append(q)
            for dtype in ("nüfus", "temettuat", "karyesi"):
                q = f'"{pv}" {dtype}'
                if q not in queries:
                    queries.append(q)
    elif place_variants:
        for pv in place_variants[:6]:
            for dtype in ("nüfus", "temettuat", "karyesi", "kazası"):
                q = f'"{pv}" {dtype}'
                if q not in queries:
                    queries.append(q)
    else:
        for nv in name_variants[:6]:
            q = f'"{nv}"'
            if q not in queries:
                queries.append(q)

    return {
        "name": name_str,
        "place": place_str,
        "name_variants": name_variants,
        "place_variants": place_variants,
        "date_window": date_window,
        "recommended_fonlar": recommended_fonlar,
        "queries": queries,
    }


def cmd_query_builder(args: argparse.Namespace) -> None:
    try:
        plan = build_archive_queries(
            name=args.name or "",
            place=args.place or "",
            year=args.year,
        )
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")

    print("=== Osmanlı Arşiv Katalog Sorgu ve Varyant Üretici (DAB / BOA) ===")
    if plan["name_variants"]:
        print(f"  Şahıs / Sülale Varyantları : {', '.join(plan['name_variants'])}")
    if plan["place_variants"]:
        print(f"  Yer Adı Varyantları        : {', '.join(plan['place_variants'])}")
    if plan["date_window"]:
        dw = plan["date_window"]
        print(
            f"  Tarih Aralığı (Yıl {dw['girdi_yili']}) : "
            f"Hicrî/Rumî {dw['onerilen_hicri_aralik']} | Miladî {dw['onerilen_miladi_aralik']}"
        )
    print(f"  Önerilen Arşiv Fonları     : {', '.join(plan['recommended_fonlar'])}")
    print("  Önerilen Katalog Arama Sorguları:")
    for idx, q in enumerate(plan["queries"], 1):
        print(f"    [{idx:02d}] {q}")


def build_lineage_mermaid(records: list[dict[str, object]]) -> dict[str, object]:
    """
    Osmanlı Nüfus / Temettuat defteri hane kayıtlarını (`hane`, `isim`, `baba_adi`, `yas`, `hicri_yil`)
    alarak Hicrî/Rumî -> Miladî tahmini doğum yıllarını hesaplar, baba-evlat bağlarını kurar ve
    doğrudan Markdown içine gömülebilecek Mermaid (`flowchart TD`) soy ağacı + tablo üretir.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("Kayıt listesi (`records`) boş olamaz.")

    enriched: list[dict[str, object]] = []
    for idx, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            raise ValueError(f"{idx}. kayıt geçerli bir sözlük (dict) değil.")
        isim = str(rec.get("isim") or "").strip()
        if not isim:
            raise ValueError(f"{idx}. kayıtta 'isim' alanı zorunludur.")
        hane = str(rec.get("hane") if rec.get("hane") is not None else "1").strip() or "1"
        baba_adi = str(rec.get("baba_adi") or "").strip()
        unvan = str(rec.get("unvan") or rec.get("lakap") or "").strip()
        yas_raw = rec.get("yas")
        yas = int(yas_raw) if yas_raw is not None and str(yas_raw).strip() != "" else None
        hicri_raw = rec.get("hicri_yil")
        hicri_yil = int(hicri_raw) if hicri_raw is not None and str(hicri_raw).strip() != "" else None

        defter_miladi_hicri = None
        defter_miladi_rumi = None
        dogum_miladi_hicri = None
        dogum_miladi_rumi = None

        if hicri_yil is not None:
            cal = hicri_miladi_cevir(hicri_yil)
            defter_miladi_hicri = int(cal["hicri_ise_miladi"])
            defter_miladi_rumi = int(cal["rumi_ise_miladi"])
            if yas is not None:
                dogum_miladi_hicri = defter_miladi_hicri - yas
                dogum_miladi_rumi = defter_miladi_rumi - yas

        enriched.append({
            "node_id": f"P{idx}",
            "hane": hane,
            "isim": isim,
            "baba_adi": baba_adi,
            "unvan": unvan,
            "yas": yas,
            "hicri_yil": hicri_yil,
            "defter_miladi_hicri": defter_miladi_hicri,
            "defter_miladi_rumi": defter_miladi_rumi,
            "dogum_miladi_hicri": dogum_miladi_hicri,
            "dogum_miladi_rumi": dogum_miladi_rumi,
        })

    # Hane bazında ve genel isim indekslemesi (baba_adi eşleştirmesi için)
    by_hane_name: dict[tuple[str, str], str] = {}
    by_name: dict[str, str] = {}
    for item in enriched:
        norm_name = str(item["isim"]).lower()
        by_hane_name[(str(item["hane"]), norm_name)] = str(item["node_id"])
        if norm_name not in by_name:
            by_name[norm_name] = str(item["node_id"])

    lines: list[str] = ["flowchart TD"]
    ancestor_nodes: dict[tuple[str, str], str] = {}
    anc_counter = 0

    def _safe_mermaid_text(text: object) -> str:
        s = _CONTROL_CHAR_RE.sub(" ", str(text or ""))
        return s.replace('"', "'").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;").strip()

    for item in enriched:
        nid = str(item["node_id"])
        hane = _safe_mermaid_text(item["hane"])
        isim = _safe_mermaid_text(item["isim"])
        unvan = _safe_mermaid_text(item["unvan"])
        label_parts = [f"Hane {hane}: {isim}"]
        if unvan:
            label_parts[0] += f" ({unvan})"
        if item["yas"] is not None:
            label_parts.append(f"Yaş: {item['yas']}")
        if item["dogum_miladi_hicri"] is not None:
            label_parts.append(f"Doğum ≈ {item['dogum_miladi_hicri']} (M)")
        elif item["defter_miladi_hicri"] is not None:
            label_parts.append(f"Defter: {item['hicri_yil']} ({item['defter_miladi_hicri']})")

        label = " | ".join(label_parts)
        lines.append(f'    {nid}["{label}"]')

    for item in enriched:
        baba = str(item["baba_adi"]).strip()
        if not baba:
            continue
        hane = str(item["hane"])
        nid = str(item["node_id"])
        norm_baba = baba.lower()
        parent_id = by_hane_name.get((hane, norm_baba)) or by_name.get(norm_baba)
        if not parent_id or parent_id == nid:
            anc_key = (hane, norm_baba)
            if anc_key not in ancestor_nodes:
                anc_counter += 1
                parent_id = f"A{anc_counter}"
                ancestor_nodes[anc_key] = parent_id
                safe_baba = _safe_mermaid_text(baba)
                safe_hane = _safe_mermaid_text(hane)
                lines.append(f'    {parent_id}["Hane {safe_hane}: {safe_baba} (Ata / Baba)"]')
            else:
                parent_id = ancestor_nodes[anc_key]
        lines.append(f"    {parent_id} --> {nid}")

    mermaid_str = "\n".join(lines)

    md_lines = [
        "### Osmanlı Arşiv Hane Soy Ağacı (Pedigree & Miladî Doğum Tablosu)",
        "",
        "| Hane | İsim | Baba Adı | Yaş (`Sinn`) | Defter Yılı (H/R) | Tahmini Doğum (Miladî Hicrî / Rumî) |",
        "|------|------|----------|--------------|-------------------|-------------------------------------|",
    ]
    for item in enriched:
        yas_str = str(item["yas"]) if item["yas"] is not None else "—"
        yil_str = (
            f"{item['hicri_yil']} ({item['defter_miladi_hicri']}/{item['defter_miladi_rumi']})"
            if item["hicri_yil"] is not None
            else "—"
        )
        dogum_str = (
            f"~{item['dogum_miladi_hicri']} / ~{item['dogum_miladi_rumi']}"
            if item["dogum_miladi_hicri"] is not None
            else "—"
        )
        md_lines.append(
            f"| {item['hane']} | {item['isim']} | {item['baba_adi'] or '—'} | {yas_str} | {yil_str} | {dogum_str} |"
        )
    md_lines.extend(["", "```mermaid", mermaid_str, "```"])
    markdown_str = "\n".join(md_lines)

    return {
        "records": enriched,
        "mermaid": mermaid_str,
        "markdown": markdown_str,
    }


def _load_records_json_arg(raw_arg: str) -> object:
    """Inline JSON dizisini veya güvenli dosya yolundan boyut-sınırlı JSON yükler."""
    raw = (raw_arg or "").strip()
    if not raw:
        raise ValueError("--records-json parametresi boş olamaz.")
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"JSON girdisi izin verilen boyutu ({MAX_JSON_BYTES} B) aşıyor.")
    # Inline JSON hızlı ayrımı
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception as exc:
            raise ValueError(f"Geçersiz JSON girdisi: {exc}") from exc

    safe_path = validate_safe_path(raw)
    if not safe_path.is_file():
        raise ValueError(f"Geçersiz JSON girdisi veya dosya bulunamadı: {raw}")
    if safe_path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"JSON dosyası izin verilen boyutu ({MAX_JSON_BYTES} B) aşıyor.")
    try:
        return json.loads(safe_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"JSON dosyası okunamadı: {exc}") from exc


def cmd_lineage_tree(args: argparse.Namespace) -> None:
    try:
        payload = _load_records_json_arg(args.records_json)
        res = build_lineage_mermaid(payload)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")

    print(res["markdown"])


def trace_migration_timeline(records: list[dict[str, object]]) -> dict[str, object]:
    """
    Osmanlı Nüfus, Temettuat, Tahrir ve Muhacir/Mübadil kayıtlarından (`kaza`, `nahiye`,
    `karye`, `yil`/`hicri_yil`/`rumi_yil`/`miladi_yil`) kronolojik göç zaman çizelgesi,
    köy adı / idari bağ çapraz referansları ve Mermaid (`flowchart LR`) göç diyagramı üretir.
    """
    if not isinstance(records, list) or not records:
        raise ValueError("Kayıt listesi (`records`) boş olamaz.")

    parsed: list[dict[str, object]] = []
    for idx, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            raise ValueError(f"{idx}. kayıt geçerli bir sözlük (dict) değil.")

        kaza = str(rec.get("kaza") or "").strip()
        nahiye = str(rec.get("nahiye") or "").strip()
        karye = str(rec.get("karye") or rec.get("koy") or rec.get("mahalle") or rec.get("place") or rec.get("yer") or "").strip()
        if not any([kaza, nahiye, karye]):
            raise ValueError(f"{idx}. kayıtta en az bir yerleşim alanı ('kaza', 'nahiye' veya 'karye') zorunludur.")

        vilayet = str(rec.get("vilayet") or rec.get("eyalet") or "").strip()
        sancak = str(rec.get("sancak") or rec.get("liva") or "").strip()
        eski_ad = str(rec.get("eski_ad") or rec.get("old_name") or rec.get("osmanlica_ad") or "").strip()
        kisi = str(rec.get("kisi") or rec.get("isim") or rec.get("sulale") or "").strip()
        olay = str(rec.get("olay") or rec.get("sebep") or rec.get("not") or rec.get("serh") or rec.get("note") or "").strip()
        fon = str(rec.get("fon") or rec.get("kaynak") or rec.get("defter") or "").strip()
        takvim_hint = str(rec.get("takvim") or "").strip().lower()

        miladi_raw = rec.get("miladi_yil") if rec.get("miladi_yil") is not None else rec.get("miladi")
        rumi_raw = rec.get("rumi_yil") if rec.get("rumi_yil") is not None else rec.get("rumi")
        hicri_raw = rec.get("hicri_yil") if rec.get("hicri_yil") is not None else rec.get("hicri")
        yil_raw = rec.get("yil") if rec.get("yil") is not None else rec.get("year")

        kaynak_yil: int | None = None
        kaynak_takvim = ""
        miladi_yil: int | None = None

        if miladi_raw is not None and str(miladi_raw).strip() != "":
            miladi_yil = int(miladi_raw)
            kaynak_yil = miladi_yil
            kaynak_takvim = "Miladî"
        elif rumi_raw is not None and str(rumi_raw).strip() != "":
            kaynak_yil = int(rumi_raw)
            cal = hicri_miladi_cevir(kaynak_yil)
            miladi_yil = int(cal["rumi_ise_miladi"])
            kaynak_takvim = "Rumî"
        elif hicri_raw is not None and str(hicri_raw).strip() != "":
            kaynak_yil = int(hicri_raw)
            cal = hicri_miladi_cevir(kaynak_yil)
            if takvim_hint == "rumi":
                miladi_yil = int(cal["rumi_ise_miladi"])
                kaynak_takvim = "Rumî"
            else:
                miladi_yil = int(cal["hicri_ise_miladi"])
                kaynak_takvim = "Hicrî"
        elif yil_raw is not None and str(yil_raw).strip() != "":
            kaynak_yil = int(yil_raw)
            if kaynak_yil < 1500 and takvim_hint != "miladi":
                cal = hicri_miladi_cevir(kaynak_yil)
                if takvim_hint == "rumi":
                    miladi_yil = int(cal["rumi_ise_miladi"])
                    kaynak_takvim = "Rumî"
                else:
                    miladi_yil = int(cal["hicri_ise_miladi"])
                    kaynak_takvim = "Hicrî"
            else:
                miladi_yil = kaynak_yil
                kaynak_takvim = "Miladî"

        parsed.append({
            "orig_index": idx,
            "vilayet": vilayet,
            "sancak": sancak,
            "kaza": kaza,
            "nahiye": nahiye,
            "karye": karye,
            "eski_ad": eski_ad,
            "kisi": kisi,
            "olay": olay,
            "fon": fon,
            "kaynak_yil": kaynak_yil,
            "kaynak_takvim": kaynak_takvim,
            "miladi_yil": miladi_yil,
            "_sort_key": (0 if miladi_yil is not None else 1, miladi_yil if miladi_yil is not None else 999999, idx),
        })

    parsed.sort(key=lambda item: item["_sort_key"])

    loc_map: dict[tuple[str, str, str], dict[str, object]] = {}
    unique_locations: list[dict[str, object]] = []
    timeline: list[dict[str, object]] = []
    karye_admins: dict[str, list[dict[str, object]]] = {}

    for step_no, item in enumerate(parsed, 1):
        kaza = str(item["kaza"])
        nahiye = str(item["nahiye"])
        karye = str(item["karye"])
        loc_key = (kaza.casefold(), nahiye.casefold(), karye.casefold())

        if loc_key not in loc_map:
            loc_id = f"L{len(unique_locations) + 1}"
            variants: list[str] = []
            if karye:
                variants.extend(_generate_spelling_variants(karye))
            if item["eski_ad"]:
                for ev in _generate_spelling_variants(str(item["eski_ad"])):
                    if ev not in variants:
                        variants.append(ev)
            loc_entry = {
                "loc_id": loc_id,
                "kaza": kaza,
                "nahiye": nahiye,
                "karye": karye,
                "eski_ad": item["eski_ad"],
                "years": [],
                "fonlar": [],
                "karye_variants": variants,
            }
            loc_map[loc_key] = loc_entry
            unique_locations.append(loc_entry)
        else:
            loc_entry = loc_map[loc_key]
            if item["eski_ad"] and not loc_entry["eski_ad"]:
                loc_entry["eski_ad"] = item["eski_ad"]

        if item["miladi_yil"] is not None and item["miladi_yil"] not in loc_entry["years"]:
            loc_entry["years"].append(item["miladi_yil"])
        if item["fon"] and item["fon"] not in loc_entry["fonlar"]:
            loc_entry["fonlar"].append(item["fon"])

        timeline_row = {k: v for k, v in item.items() if k != "_sort_key"}
        timeline_row["step"] = step_no
        timeline_row["loc_id"] = loc_entry["loc_id"]
        timeline.append(timeline_row)

        if karye:
            karye_admins.setdefault(karye.casefold(), []).append({
                "karye": karye,
                "kaza": kaza,
                "nahiye": nahiye,
                "miladi_yil": item["miladi_yil"],
                "loc_id": loc_entry["loc_id"],
            })

    transitions: list[dict[str, object]] = []
    cross_references: list[dict[str, object]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for prev, curr in zip(timeline, timeline[1:]):
        if prev["loc_id"] == curr["loc_id"]:
            continue
        same_village = (
            bool(prev["karye"])
            and bool(curr["karye"])
            and str(prev["karye"]).casefold() == str(curr["karye"]).casefold()
        )
        t_type = "IDARI_BAG_DEGISIKLIGI" if same_village else "GOC_HAREKETI"
        t_label = "İdari Bağ Değişikliği" if same_village else "Göç / Nakil"
        y1 = prev["miladi_yil"]
        y2 = curr["miladi_yil"]
        if y1 is not None and y2 is not None:
            year_span = f"{y1}->{y2}" if y1 != y2 else str(y2)
        elif y2 is not None:
            year_span = str(y2)
        else:
            year_span = "Tarihsiz"

        edge_desc_parts = [year_span, t_label]
        if curr["olay"]:
            edge_desc_parts.append(str(curr["olay"]))
        elif curr["fon"]:
            edge_desc_parts.append(str(curr["fon"]))

        transition = {
            "from_loc_id": prev["loc_id"],
            "to_loc_id": curr["loc_id"],
            "type": t_type,
            "label": t_label,
            "from_year": y1,
            "to_year": y2,
            "from_place": " / ".join(x for x in [str(prev["kaza"]), str(prev["nahiye"]), str(prev["karye"])] if x),
            "to_place": " / ".join(x for x in [str(curr["kaza"]), str(curr["nahiye"]), str(curr["karye"])] if x),
            "edge_text": " : ".join(edge_desc_parts),
        }
        transitions.append(transition)

    # Köy adı & idari bağ çapraz referansları
    for _, occurrences in karye_admins.items():
        distinct_admins = []
        for occ in occurrences:
            sig = (occ["kaza"], occ["nahiye"])
            if sig not in distinct_admins:
                distinct_admins.append(sig)
        if len(distinct_admins) > 1:
            cross_references.append({
                "type": "IDARI_BAG_DEGISIKLIGI",
                "karye": occurrences[0]["karye"],
                "jurisdictions": [
                    {"kaza": kz, "nahiye": nh} for kz, nh in distinct_admins
                ],
                "years": [occ["miladi_yil"] for occ in occurrences if occ["miladi_yil"] is not None],
            })

    for loc in unique_locations:
        if loc["eski_ad"]:
            cross_references.append({
                "type": "ESKI_AD_ESLESMESI",
                "karye": loc["karye"] or loc["kaza"],
                "eski_ad": loc["eski_ad"],
                "variants": loc["karye_variants"],
            })

    def _safe(text: object) -> str:
        s = _CONTROL_CHAR_RE.sub(" ", str(text or ""))
        return s.replace('"', "'").replace("|", "/").replace("`", "'").replace("<", "&lt;").replace(">", "&gt;").strip()

    lines: list[str] = ["flowchart LR"]
    for loc in unique_locations:
        parts = [x for x in [str(loc["kaza"]), str(loc["nahiye"]), str(loc["karye"])] if x]
        title = " / ".join(parts)
        if loc["eski_ad"]:
            title += f" (Eski: {loc['eski_ad']})"
        if loc["years"]:
            yrs = ", ".join(str(y) for y in loc["years"])
            title += f" [{yrs}]"
        lines.append(f'    {loc["loc_id"]}["{_safe(title)}"]')

    for tr in transitions:
        ekey = (str(tr["from_loc_id"]), str(tr["to_loc_id"]), str(tr["edge_text"]))
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            lines.append(f'    {tr["from_loc_id"]} -->|"{_safe(tr["edge_text"])}"| {tr["to_loc_id"]}')

    mermaid_str = "\n".join(lines)

    md_lines = [
        "### Coğrafi Göç ve Köy / İdari Bağ Zaman Çizelgesi",
        "",
        "| Sıra | Yıl (Kaynak / Miladî) | Kaza | Nahiye | Karye / Mahalle | Eski Ad | Kişi / Olay / Fon |",
        "|------|-----------------------|------|--------|-----------------|---------|-------------------|",
    ]
    for row in timeline:
        if row["miladi_yil"] is not None and row["kaynak_yil"] is not None and row["kaynak_takvim"] != "Miladî":
            yil_cell = f"{row['kaynak_takvim'][0]}.{row['kaynak_yil']} ({row['miladi_yil']})"
        elif row["miladi_yil"] is not None:
            yil_cell = str(row["miladi_yil"])
        else:
            yil_cell = "—"
        detail_cell = " · ".join(x for x in [str(row["kisi"]), str(row["olay"]), str(row["fon"])] if x) or "—"
        md_lines.append(
            f"| {row['step']} | {yil_cell} | {row['kaza'] or '—'} | {row['nahiye'] or '—'} | "
            f"{row['karye'] or '—'} | {row['eski_ad'] or '—'} | {detail_cell} |"
        )

    if cross_references:
        md_lines.extend(["", "#### Köy Adı ve İdari Bağ Çapraz Başvuruları"])
        for cr in cross_references:
            if cr["type"] == "IDARI_BAG_DEGISIKLIGI":
                chain = " -> ".join(f"{j['kaza']}/{j['nahiye'] or '—'}" for j in cr["jurisdictions"])
                md_lines.append(f"- **{cr['karye']}** idari bağlılık değişimi: {chain}")
            elif cr["type"] == "ESKI_AD_ESLESMESI":
                md_lines.append(
                    f"- **{cr['karye']}** (Eski Ad: *{cr['eski_ad']}*) katalog varyantları: "
                    f"{', '.join(cr['variants'][:6])}"
                )

    md_lines.extend(["", "```mermaid", mermaid_str, "```"])
    markdown_str = "\n".join(md_lines)

    return {
        "timeline": timeline,
        "transitions": transitions,
        "unique_locations": unique_locations,
        "cross_references": cross_references,
        "mermaid": mermaid_str,
        "markdown": markdown_str,
    }


def cmd_migration_map(args: argparse.Namespace) -> None:
    try:
        payload = _load_records_json_arg(args.records_json)
        res = trace_migration_timeline(payload)
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")

    print(res["markdown"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Osmanlıca arşiv defteri varak ayırma ve 1:1 piksel kesit aracı.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="Klasördeki .zip ve görüntü dosyalarını açıp boyutlarıyla listeler.")
    p_inv.add_argument("--input", required=True, help="İndirilen arşiv dosyalarının bulunduğu klasör")
    p_inv.add_argument("--output", required=True, help="Açılan dosyaların ve kesitlerin yazılacağı klasör")
    p_inv.set_defaults(func=cmd_inventory)

    p_spl = sub.add_parser("split", help="Çift varaklı defter pozunu Sağ (R) ve Sol (L) sayfalara ve yatay okuma şeritlerine böler.")
    p_spl.add_argument("--image", required=True, help="Kaynak defter görüntüsü (.jpg/.png)")
    p_spl.add_argument("--output", required=True, help="Çıktı klasörü")
    p_spl.add_argument("--bands", action="store_true", help="Her sayfayı üst/orta/alt 3 okuma şeridine de böl")
    p_spl.set_defaults(func=cmd_split)

    p_crp = sub.add_parser("crop", help="Belirli bir hane veya satırdan 1:1 (veya büyütülmüş) kanıt kesiti alır.")
    p_crp.add_argument("--image", required=True, help="Kaynak görüntü dosyası")
    p_crp.add_argument("--box", required=True, help="'x1,y1,x2,y2' (0.0-1.0 oran veya piksel koordinatı)")
    p_crp.add_argument("--output", required=True, help="Kaydedilecek kesit dosya yolu (.png/.jpg)")
    p_crp.add_argument("--zoom", type=float, default=1.0, help="Büyütme katsayısı (örn. 2.0 veya 3.0)")
    p_crp.add_argument("--nearest", action="store_true", help="Harf noktalarını keskin görmek için piksel (NEAREST) büyütme kullan")
    p_crp.set_defaults(func=cmd_crop)

    p_cal = sub.add_parser("calendar", help="Hicrî veya Rumî defter yılını Miladî yıla çevirir (örn. --year 1293).")
    p_cal.add_argument("--year", type=int, required=True, help="Defterdeki Hicrî veya Rumî yıl (örn. 1260, 1293, 1320)")
    p_cal.add_argument("--birth-age", type=int, default=None, help="Defterde yazan yaş (`sinn`) ile tahmini doğum yılını hesapla")
    p_cal.set_defaults(func=cmd_calendar)

    p_qb = sub.add_parser("query-builder", help="Osmanlıca yer/şahıs imla varyantlarını ve DAB katalog arama sorgularını üretir.")
    p_qb.add_argument("--name", default="", help="Şahıs, baba adı veya sülale/lakap (örn. 'Mehmedoğlu', 'Kara')")
    p_qb.add_argument("--place", default="", help="Karye, kaza veya sancak adı (örn. 'Muğla', 'Üsküdar', 'Çemişgezek')")
    p_qb.add_argument("--year", type=int, default=None, help="Tahmini Hicrî/Rumî defter yılı (örn. 1260, 1293)")
    p_qb.set_defaults(func=cmd_query_builder)

    p_lt = sub.add_parser("lineage-tree", help="Hane kayıtlarından (`hane`, `isim`, `baba_adi`, `yas`, `hicri_yil`) Miladî doğum yıllarını ve Mermaid soy ağacını üretir.")
    p_lt.add_argument("--records-json", required=True, help="JSON dizisi veya JSON dosya yolu")
    p_lt.set_defaults(func=cmd_lineage_tree)

    p_mm = sub.add_parser("migration-map", help="Kaza/nahiye/karye göç ve idari bağ kayıtlarından kronolojik zaman çizelgesi ve Mermaid flowchart LR diyagramı üretir.")
    p_mm.add_argument("--records-json", required=True, help="JSON dizisi veya JSON dosya yolu")
    p_mm.set_defaults(func=cmd_migration_map)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
