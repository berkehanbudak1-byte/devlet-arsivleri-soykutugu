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

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

# Güvenlik sınırları (Upload2Shell / decompression bomb / zip bomb / çıktı boyut koruması)
MAX_IMAGE_PIXELS = 16_000_000  # 16 MP üst sınır (decompression bomb ve bellek tavanı)
ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "TIFF")  # EPS/PostScript (Ghostscript) delegasyonunu engeller
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB ham görsel bayt tavanı (kayıpsız 16 MP arşiv TIFF/taramalar dahil)
FORBIDDEN_IMAGE_SIGNATURES = (b"%!PS", b"\xc5\xd0\xd3\xc6", b"%PDF")  # PostScript/EPS (ASCII & ikili) ve PDF

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
_MERMAID_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]")
_UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]+')
_DANGEROUS_URI_SCHEME_RE = re.compile(r"(?i)\b(?:javascript|vbscript|data)\s*:")
_MERMAID_DIRECTIVE_TOKEN_RE = re.compile(
    r"(?i)\b(?:click|href|classdef|linkstyle|callback|acctitle|accdescr)\b"
)


def _tr_lower(text: object) -> str:
    """Türkçe İ (U+0130), ayrık I+U+0307 ve I karakterlerini güvenli biçimde küçük harfe çevirir."""
    raw = str(text or "")
    return raw.replace("I\u0307", "i").replace("İ", "i").replace("I", "ı").lower()


def sanitize_mermaid_label(text: object) -> str:
    """
    Mermaid düğüm/kenar etiketlerini XSS, HTML enjeksiyonu ve Mermaid
    direktif/etkileşim (`click`, `href`, `%%{init:...}%%`, satır sonu, tırnak,
    köşeli/süslü parantez, noktalı virgül, dikey çubuk, ters tırnak, `#entity;`)
    kaçışlarına karşı katı biçimde temizler.
    """
    s = _MERMAID_CONTROL_RE.sub(" ", str(text or ""))
    # Mermaid yorum/direktif öneklerini (`%%`, `%%{...}%%`) etkisizleştir
    s = re.sub(r"%{2,}", "%", s)
    # Yapısal Mermaid/Markdown kaçış karakterlerini HTML entity öncesi dönüştür
    s = (
        s.replace("\\", "/")
        .replace(";", ",")
        .replace("|", "/")
        .replace("{", "(")
        .replace("}", ")")
        .replace("`", " ")
        .replace("#", "No.")
    )
    # Tehlikeli URI şemalarını ve etkileşimli Mermaid anahtar kelimelerini etkisizleştir
    s = _DANGEROUS_URI_SCHEME_RE.sub("", s)
    s = _MERMAID_DIRECTIVE_TOKEN_RE.sub("", s)
    # HTML, tırnak ve köşeli parantez kaçışları (& her zaman ilk sırada;
    # [ ve ] Mermaid düğüm sözdizimini kırmamak için &#91;/&#93; olarak kaçışlanır)
    s = (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )
    return re.sub(r"\s+", " ", s).strip()


def sanitize_markdown_cell(text: object) -> str:
    """Markdown tablo hücrelerini ve liste metinlerini HTML/tablo/kod-bloğu enjeksiyonuna karşı temizler."""
    s = _MERMAID_CONTROL_RE.sub(" ", str(text or ""))
    s = (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "/")
        .replace("`", "'")
    )
    return re.sub(r"\s+", " ", s).strip()


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


def validate_image_magic_bytes(data: bytes | bytearray, source_label: str = "") -> None:
    """
    Ham görüntü başlığında Upload2Shell (CVE-2026-125396) riskine yol açan
    PostScript/EPS (ASCII ve ikili) veya PDF sihirli baytlarını Pillow
    yüklenmeden önce saf stdlib ile reddeder.
    """
    label = f" ({source_label})" if source_label else ""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError(f"Görüntü verisi bayt dizisi olmalıdır{label}.")
    raw = bytes(data)
    if not raw:
        raise ValueError(f"Boş görüntü verisi reddedildi{label}.")
    stripped = raw.lstrip(b" \t\r\n\x0b\x0c\x00")
    if not stripped:
        raise ValueError(f"Geçersiz veya yalnızca boşluk/NUL içeren görüntü verisi reddedildi{label}.")
    if stripped.startswith(FORBIDDEN_IMAGE_SIGNATURES):
        raise ValueError(
            f"Güvenlik nedeniyle yasaklı görüntü başlığı (PostScript/EPS/PDF) reddedildi{label}."
        )


def _validate_image_file_header(img_path: Path, *, max_bytes: int = MAX_IMAGE_BYTES) -> None:
    """
    Görüntü dosyasının normal dosya varlığını, bayt üst sınırını ve ilk 4096
    bayttaki sihirli baytları Pillow çağrılmadan önce saf stdlib ile doğrular.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes pozitif bir tam sayı olmalıdır.")
    path = Path(img_path)
    try:
        st = path.stat()
    except OSError as exc:
        raise ValueError(f"Görüntü dosyası bulunamadı veya okunamadı ({path.name}): {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"Görüntü yolu normal bir dosya olmalıdır: {path.name}")
    if st.st_size <= 0:
        raise ValueError(f"Boş görüntü dosyası reddedildi: {path.name}")
    if st.st_size > max_bytes:
        raise ValueError(
            f"Görüntü dosya boyutu ({st.st_size} B) izin verilen üst sınırı ({max_bytes} B) aşıyor: {path.name}"
        )
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        raise ValueError(f"Görüntü başlığı okunamadı ({path.name}): {exc}") from exc
    validate_image_magic_bytes(head, str(path.name))


def _harden_pillow(pil_image: Any) -> Any:
    """
    Pillow piksel tavanını (16 MP) uygular ve Upload2Shell (CVE-2026-125396)
    riskine karşı Ghostscript delegasyonu yapan EPS eklentisini kapatır.
    """
    pil_image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    pil_image.init()
    pil_image.OPEN.pop("EPS", None)
    while "EPS" in pil_image.ID:
        pil_image.ID.remove("EPS")
    return pil_image


def _require_pil_image() -> Any:
    """Pillow (PIL.Image) modülünü yalnızca görüntü komutlarında tembel (lazy) yükler ve sertleştirir."""
    try:
        from PIL import Image as pil_image
    except ImportError:
        sys.exit("Hata: 'Pillow' kütüphanesi kurulu değil. Kurmak için: pip install Pillow")
    return _harden_pillow(pil_image)


def open_image_safely(
    img_path: Path,
    *,
    max_pixels: int | None = None,
    max_bytes: int = MAX_IMAGE_BYTES,
) -> Any:
    """
    Görüntüyü Upload2Shell (CVE-2026-125396) ve decompression bomb koruması
    etkin biçimde (magic-byte + boyut + format izin listesi + 16 MP tavan + load) açar.
    """
    if max_pixels is None:
        limit = MAX_IMAGE_PIXELS
    else:
        if isinstance(max_pixels, bool) or not isinstance(max_pixels, int) or max_pixels <= 0:
            raise ValueError("max_pixels pozitif bir tam sayı olmalıdır.")
        limit = min(int(max_pixels), MAX_IMAGE_PIXELS)

    path = Path(img_path)
    _validate_image_file_header(path, max_bytes=max_bytes)

    pil_image = _require_pil_image()
    from PIL import UnidentifiedImageError

    try:
        im = pil_image.open(path, formats=ALLOWED_IMAGE_FORMATS)
    except pil_image.DecompressionBombError as exc:
        raise ValueError(
            f"Görüntü piksel üst sınırını ({limit} px) aşıyor (decompression bomb koruması): {exc}"
        ) from exc
    except UnidentifiedImageError as exc:
        raise ValueError(
            f"Desteklenmeyen veya geçersiz görüntü formatı ({path.name}); yalnızca {', '.join(ALLOWED_IMAGE_FORMATS)} desteklenir."
        ) from exc
    except OSError as exc:
        raise ValueError(f"Görüntü dosyası açılamadı ({path.name}): {exc}") from exc

    try:
        fmt = getattr(im, "format", None)
        if fmt is not None and str(fmt).upper() not in ALLOWED_IMAGE_FORMATS:
            raise ValueError(
                f"Desteklenmeyen veya geçersiz görüntü formatı ({fmt}); yalnızca {', '.join(ALLOWED_IMAGE_FORMATS)} desteklenir."
            )
        pixels = im.width * im.height
        if pixels > limit:
            raise ValueError(
                f"Görüntü piksel üst sınırını ({pixels} > {limit} px) aşıyor (decompression bomb koruması)."
            )
        try:
            im.load()
        except pil_image.DecompressionBombError as exc:
            raise ValueError(
                f"Görüntü piksel üst sınırını ({limit} px) aşıyor (decompression bomb koruması): {exc}"
            ) from exc
        except UnidentifiedImageError as exc:
            raise ValueError(
                f"Desteklenmeyen veya geçersiz görüntü formatı ({path.name}); yalnızca {', '.join(ALLOWED_IMAGE_FORMATS)} desteklenir."
            ) from exc
        except OSError as exc:
            raise ValueError(f"Görüntü verisi çözümlenemedi ({path.name}): {exc}") from exc
    except Exception:
        im.close()
        raise
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
                                is_image_member = Path(normalized_posix).suffix.lower() in IMAGE_EXTS
                                if is_image_member and member.file_size > MAX_IMAGE_BYTES:
                                    raise ValueError(
                                        f"ZIP içindeki görüntü boyut sınırını ({MAX_IMAGE_BYTES} B) aşıyor: {raw_member_name}"
                                    )
                                min_magic_len = max(len(sig) for sig in FORBIDDEN_IMAGE_SIGNATURES)
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                try:
                                    with zf.open(member, "r") as src_fh, open(dest, "wb") as dst_fh:
                                        written = 0
                                        header_buf = bytearray()
                                        header_validated = False
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
                                            if is_image_member and written > MAX_IMAGE_BYTES:
                                                raise ValueError(
                                                    f"ZIP içindeki görüntü gerçek boyut sınırını ({MAX_IMAGE_BYTES} B) aşıyor: {raw_member_name}"
                                                )
                                            if actual_total_written > MAX_ZIP_TOTAL_BYTES:
                                                raise ValueError(
                                                    f"ZIP toplam gerçek açılmış boyut sınırını ({MAX_ZIP_TOTAL_BYTES} B) aşıyor: {fpath.name}"
                                                )
                                            if is_image_member and not header_validated:
                                                remaining = 4096 - len(header_buf)
                                                if remaining > 0:
                                                    header_buf.extend(chunk[:remaining])
                                                stripped = bytes(header_buf).lstrip(b" \t\r\n\x0b\x0c\x00")
                                                if len(stripped) >= min_magic_len or len(header_buf) >= 4096:
                                                    validate_image_magic_bytes(header_buf, normalized_posix)
                                                    header_validated = True
                                            dst_fh.write(chunk)
                                        if is_image_member and not header_validated:
                                            validate_image_magic_bytes(header_buf, normalized_posix)
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
                pil_image = _require_pil_image()
                resample = pil_image.Resampling.NEAREST if args.nearest else pil_image.Resampling.LANCZOS
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


def _escape_mermaid_label(text: object, *, escape_pipe: bool = False) -> str:
    """Geriye dönük uyumluluk sarmalayıcısı; tüm Mermaid kaçışlarını `sanitize_mermaid_label` üzerinden yürütür."""
    _ = escape_pipe  # sanitize_mermaid_label dikey çubuğu (|) her zaman güvenli biçimde kaçışlar
    return sanitize_mermaid_label(text)


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

    for item in enriched:
        nid = str(item["node_id"])
        hane = sanitize_mermaid_label(item["hane"]) or "1"
        isim = sanitize_mermaid_label(item["isim"]) or "—"
        unvan = sanitize_mermaid_label(item["unvan"])
        label_parts = [f"Hane {hane}: {isim}"]
        if unvan:
            label_parts[0] += f" ({unvan})"
        if item["yas"] is not None:
            label_parts.append(f"Yaş: {item['yas']}")
        if item["dogum_miladi_hicri"] is not None:
            label_parts.append(f"Doğum ≈ {item['dogum_miladi_hicri']} (M)")
        elif item["defter_miladi_hicri"] is not None:
            label_parts.append(f"Defter: {item['hicri_yil']} ({item['defter_miladi_hicri']})")

        label = " · ".join(label_parts)
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
                safe_baba = sanitize_mermaid_label(baba) or "—"
                safe_hane = sanitize_mermaid_label(hane) or "1"
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
        md_hane = sanitize_markdown_cell(item["hane"]) or "—"
        md_isim = sanitize_markdown_cell(item["isim"]) or "—"
        md_baba = sanitize_markdown_cell(item["baba_adi"]) or "—"
        md_lines.append(
            f"| {md_hane} | {md_isim} | {md_baba} | {yas_str} | {yil_str} | {dogum_str} |"
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

    lines: list[str] = ["flowchart LR"]
    for loc in unique_locations:
        parts = [
            sanitize_mermaid_label(x)
            for x in [loc["kaza"], loc["nahiye"], loc["karye"]]
            if str(x).strip()
        ]
        parts = [p for p in parts if p]
        title = " / ".join(parts) or "Yerleşim"
        if loc["eski_ad"]:
            safe_old = sanitize_mermaid_label(loc["eski_ad"])
            if safe_old:
                title += f" (Eski: {safe_old})"
        if loc["years"]:
            yrs = ", ".join(str(y) for y in loc["years"])
            title += f" ({yrs})"
        lines.append(f'    {loc["loc_id"]}["{title}"]')

    for tr in transitions:
        ekey = (str(tr["from_loc_id"]), str(tr["to_loc_id"]), str(tr["edge_text"]))
        if ekey not in seen_edges:
            seen_edges.add(ekey)
            safe_edge = sanitize_mermaid_label(tr["edge_text"]) or "Geçiş"
            lines.append(f'    {tr["from_loc_id"]} -->|"{safe_edge}"| {tr["to_loc_id"]}')

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
        detail_parts = [
            sanitize_markdown_cell(x)
            for x in [row["kisi"], row["olay"], row["fon"]]
            if str(x).strip()
        ]
        detail_cell = " · ".join(p for p in detail_parts if p) or "—"
        md_kaza = sanitize_markdown_cell(row["kaza"]) or "—"
        md_nahiye = sanitize_markdown_cell(row["nahiye"]) or "—"
        md_karye = sanitize_markdown_cell(row["karye"]) or "—"
        md_eski = sanitize_markdown_cell(row["eski_ad"]) or "—"
        md_lines.append(
            f"| {row['step']} | {yil_cell} | {md_kaza} | {md_nahiye} | "
            f"{md_karye} | {md_eski} | {detail_cell} |"
        )

    if cross_references:
        md_lines.extend(["", "#### Köy Adı ve İdari Bağ Çapraz Başvuruları"])
        for cr in cross_references:
            if cr["type"] == "IDARI_BAG_DEGISIKLIGI":
                chain = " -> ".join(
                    f"{sanitize_markdown_cell(j['kaza']) or '—'}/{sanitize_markdown_cell(j['nahiye']) or '—'}"
                    for j in cr["jurisdictions"]
                )
                md_lines.append(f"- **{sanitize_markdown_cell(cr['karye']) or '—'}** idari bağlılık değişimi: {chain}")
            elif cr["type"] == "ESKI_AD_ESLESMESI":
                safe_variants = [sanitize_markdown_cell(v) for v in cr["variants"][:6] if str(v).strip()]
                md_lines.append(
                    f"- **{sanitize_markdown_cell(cr['karye']) or '—'}** "
                    f"(Eski Ad: *{sanitize_markdown_cell(cr['eski_ad']) or '—'}*) katalog varyantları: "
                    f"{', '.join(safe_variants)}"
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


# 1:1 Arşiv Atıf Doğrulama (verify-citations) Desenleri
_CITATION_PLACEHOLDER_VALUES = {
    "",
    "-",
    "—",
    "–",
    "?",
    "yok",
    "none",
    "null",
    "n/a",
    "na",
    "tbd",
    "todo",
    "unknown",
    "bilinmiyor",
    "belirsiz",
    "tahmin",
    "tahmini",
    "rivayet",
    "aile rivayeti",
    "söylenti",
    "soylenti",
    "internet",
    "wikipedia",
    "chatgpt",
    "genel tarih",
}

_UNVERIFIED_MARKERS_RE = re.compile(
    r"(?:"
    r"doğrulanmadı|dogrulanmadi|doğrulanmamış|dogrulanmamis|"
    r"teyit\s+edilmedi|teyitsiz|"
    r"şüpheli\s+okuma|supheli\s+okuma|"
    r"\bunverified\b"
    r")"
)

_ARCHIVAL_FOND_PATTERNS: list[re.Pattern[str]] = [
    # BOA Defter Serileri
    re.compile(
        r"\b(?:NFS\.d|ML\.VRD\.TMT\.d|M[ŞS]H\.[ŞS]SC\.d|MAD\.d|TT\.d|TK\.GM\.d|TKG\.KK(?:\.TTd)?|"
        r"D\.+CRD\.d|ML\.CRD\.d|EV\.(?:HMH\.)?d)\.{0,3}(?:\s*(?:No\.?|Nr\.?|:|#)?\s*\d[\dA-Za-z/\-\.]*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bA\.\{?(?:RSK|DVNS\.?MHM)\.d\.{0,3}(?:\s*(?:No\.?|Nr\.?|:|#)?\s*\d[\dA-Za-z/\-\.]*)?",
        re.IGNORECASE,
    ),
    # BOA Belge Fonları
    re.compile(
        r"\b(?:DH\.(?:SN\.+THR|SN\.+M|İ\.UM|I\.UM|TMIK\.[SM]|İD|ID|MKT|EUM|ŞFR|SFR)|"
        r"A\.\}?MKT\.(?:DV|MVL|NZD|UM|MHM)|"
        r"Y\.+PRK\.[A-ZÇĞİÖŞÜ]+|"
        r"[İI]\.+DH|[İI]\.+MMS|"
        r"C\.+(?:DH|EV|TZ|ADL|ML))\.{0,3}(?:\s*(?:No\.?|Nr\.?|:|#)?\s*\d[\dA-Za-z/\-\.]*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:MVL|BEO|HAT|ŞD|SD)\.?[\s,:/#\-]+\d[\dA-Za-z/\-\.]*",
        re.IGNORECASE,
    ),
    # BCA (Cumhuriyet Arşivi) Fonları
    re.compile(
        r"\bBCA\b[\s,:;\-]*\d+(?:[-.]\d+){1,}(?:[\s,/\-]+\d[\dA-Za-z/\-\.]*)?",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:130[-.]16[-.]13[-.]2|272[-.]\d+[-.]\d+[-.]\d+|30[-.](?:18|11|10)[-.]\d+[-.]\d+|180[-.]9[-.]\d+[-.]\d+)\b"
    ),
    # Tamamlayıcı Resmi Tarih Arşivleri
    re.compile(
        r"\b(?:kadisicilleri\.org|[İI]SAM[\s,:/\-]+(?:Şer['’]?iyye|Ser['’]?iyye|Sicil|Kadı)|"
        r"MSB[\s,:;\-]+(?:110[-.]9|[ŞS]ehit)|[ŞS]ehit\s+Bilgi\s+Kap[ıi]s[ıi]|"
        r"(?:TKGM|Kuy[ûu]d[\-\s]?[ıi]\s+Kad[îi]me|VGM)[\s,:;\-]+(?:defter|kay[ıi]t|no\.?|\d[\d/\-\.]*))",
        re.IGNORECASE,
    ),
]

_EDEVLET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\be[\-\s]?devlet\b", re.IGNORECASE),
    re.compile(r"\balt[\-\s]?[üu]st\s+soy\b", re.IGNORECASE),
    re.compile(r"\b[iİı]sim\s+denklik\b", re.IGNORECASE),
    re.compile(r"\bnv[iİı]gm\b", re.IGNORECASE),
    re.compile(r"turkiye\.gov\.tr/nvi-[a-z0-9\-]+", re.IGNORECASE),
    re.compile(r"\bsicill[\-\s]?i\s+n[üu]fus\b", re.IGNORECASE),
    re.compile(r"\b(?:esas\s+k[üu]t[üu]k|n[üu]fus\s+k[üu]t[üu][ğg][üu]|vukuatl[ıi]\s+n[üu]fus)\b", re.IGNORECASE),
]

_ARCHIVAL_LOCATOR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:fond?|fon\s*kodu|kutu|dosya|g[öo]mlek|defter(?:\s*no)?|varak|vr\.?|poz|belge(?:\s*no)?|"
        r"h[üu]k[üu]m(?:\s*no)?|sicil(?:\s*no)?|cilt|k[üu]t[üu]k|barkod|box|folder|folio)\b"
        r"\s*[:=#.\-]?\s*(?:\d+[A-Za-z]?|[A-Za-z]{1,4}[-/]\d+)(?:[-/.]\d+[A-Za-z]?)*",
        re.IGNORECASE,
    ),
    re.compile(r"\bmi\s*[:=#]\s*\d+(?:[-–]\d+)?\b", re.IGNORECASE),
]

_PRIMARY_CITATION_KEYS = (
    "fon",
    "fon_kodu",
    "kaynak",
    "arsiv",
    "archive",
    "source",
    "citation",
    "citations",
    "ref",
    "referans",
    "edevlet_ref",
    "e_devlet_ref",
    "belge",
    "defter",
    "kaynakca",
)

_LOCATOR_KEYS = (
    "defter_no",
    "dosya",
    "gomlek",
    "gömlek",
    "kutu",
    "box",
    "folder",
    "varak",
    "vr",
    "poz",
    "mi",
    "belge_no",
    "barkod",
    "yer_bilgisi",
    "sicil_no",
    "cilt",
    "kutuk",
    "kütük",
)

_STATUS_KEYS = (
    "durum",
    "status",
    "kanit_seviyesi",
    "evidence_level",
    "not",
    "note",
    "serh",
    "aciklama",
)

_MD_CLAIM_HINT_RE = re.compile(
    r"(?:\b1[4-9]\d{2}\b|\b20\d{2}\b|\b[HR]\.\s*\d{3,4}\b|"
    r"doğdu|dogdu|doğum|dogum|vefat|öldü|oldu|oğlu|oglu|kızı|kizi|"
    r"\bbin\b|\bbinti\b|veled[\-\s]?i|\bhane\b|karye|köy|koy|mahalle|"
    r"göç|goc|mübadil|mubadil|muhacir|dede|\bata\b|kuşak|kusak|sülale|sulale|"
    r"lakap|baba|anne|iskân|iskan|tahrir|defter|yerleşti|yerlesti|yaşadı|yasadi|"
    r"\[kesin|\[güçlü|\[guclu|\[isim|doğrulanmadı|dogrulanmadi|\[\^[^\]]+\])",
    re.IGNORECASE,
)

_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_MD_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
_MD_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_MD_TOC_LINK_RE = re.compile(r"^\[[^\]]+\]\(#[^)]+\)\s*$")


def _flatten_citation_value(val: object, key_hint: str = "") -> list[str]:
    """Kayıttaki atıf/yer bilgisi alanlarını (str, int, list, dict) düz metin listesine çevirir."""
    out: list[str] = []
    if val is None or isinstance(val, bool):
        return out
    if isinstance(val, dict):
        for k, v in val.items():
            out.extend(_flatten_citation_value(v, key_hint=str(k)))
        return out
    if isinstance(val, (list, tuple)):
        for item in val:
            out.extend(_flatten_citation_value(item, key_hint=key_hint))
        return out
    s = str(val).strip()
    if not s or _tr_lower(s) in _CITATION_PLACEHOLDER_VALUES:
        return out
    if key_hint:
        out.append(f"{key_hint}: {s}")
    else:
        out.append(s)
    return out


def _is_explicitly_unverified(text: str, rec: dict[str, object] | None = None) -> bool:
    """Kaydın veya rapor satırının açıkça 'doğrulanmadı' / 'unverified' olarak işaretlenip işaretlenmediğini denetler."""
    if rec is not None:
        if rec.get("verified") is False or rec.get("dogrulandi") is False:
            return True
        for key in _STATUS_KEYS + _PRIMARY_CITATION_KEYS:
            val = rec.get(key)
            if val is not None and not isinstance(val, bool):
                if _UNVERIFIED_MARKERS_RE.search(_tr_lower(val)):
                    return True
    return bool(_UNVERIFIED_MARKERS_RE.search(_tr_lower(text)))


def _extract_archival_citations(text: str) -> tuple[list[str], bool]:
    """
    Metin içindeki 1:1 arşiv fonu, kutu/dosya/gömlek/defter/varak/poz yer bilgisi ve
    e-Devlet / NVİGM referanslarını deterministik olarak çıkarır.
    Dönüş: (citations, has_locator)
    """
    text = str(text or "").replace("I\u0307", "İ")
    citations: list[str] = []
    has_locator = False

    def _add(tok: str) -> None:
        cleaned = re.sub(r"\s+", " ", tok).strip(" ,.;:()[]{}")
        if cleaned and cleaned not in citations:
            citations.append(cleaned)

    for pat in _ARCHIVAL_FOND_PATTERNS:
        for m in pat.finditer(text):
            matched = m.group(0)
            _add(matched)
            if re.search(r"\d", matched):
                has_locator = True

    for pat in _EDEVLET_PATTERNS:
        for m in pat.finditer(text):
            _add(m.group(0))
            has_locator = True

    for pat in _ARCHIVAL_LOCATOR_PATTERNS:
        for m in pat.finditer(text):
            _add(m.group(0))
            has_locator = True

    return citations, has_locator


def _build_record_subject(rec: dict[str, object], idx: int) -> str:
    parts: list[str] = []
    name = str(rec.get("isim") or rec.get("kisi") or rec.get("ad") or rec.get("name") or "").strip()
    if name:
        parts.append(name)
    baba = str(rec.get("baba_adi") or "").strip()
    if baba:
        parts.append(f"baba: {baba}")
    place = str(rec.get("karye") or rec.get("koy") or rec.get("mahalle") or rec.get("kaza") or "").strip()
    if place:
        parts.append(place)
    claim_txt = str(rec.get("claim") or rec.get("iddia") or rec.get("olay") or "").strip()
    if claim_txt and not parts:
        parts.append(claim_txt[:80])
    hane = rec.get("hane")
    if hane is not None and str(hane).strip():
        parts.append(f"Hane {str(hane).strip()}")
    return " · ".join(parts) if parts else f"Kayıt #{idx}"


def _audit_dataset_records(
    records: list[dict[str, object]],
    *,
    require_locator: bool = False,
) -> dict[str, object]:
    if not records:
        raise ValueError("Denetlenecek kayıt listesi boş olamaz.")

    claims: list[dict[str, object]] = []
    warnings: list[str] = []

    for idx, rec in enumerate(records, 1):
        if not isinstance(rec, dict):
            raise ValueError(f"{idx}. kayıt geçerli bir sözlük (dict) değil.")

        citation_chunks: list[str] = []
        for k in _PRIMARY_CITATION_KEYS:
            if k in rec:
                citation_chunks.extend(_flatten_citation_value(rec.get(k)))
        for k in _LOCATOR_KEYS:
            if k in rec:
                citation_chunks.extend(_flatten_citation_value(rec.get(k), key_hint=k))

        citation_blob = " | ".join(citation_chunks)
        citations, has_locator = _extract_archival_citations(citation_blob)
        explicitly_unverified = _is_explicitly_unverified(citation_blob, rec=rec)
        subject = _build_record_subject(rec, idx)
        loc = f"record[{idx}]"

        if explicitly_unverified:
            status = "EXPLICITLY_UNVERIFIED"
            reason = "Açıkça 'doğrulanmadı' olarak işaretlenmiş (uydurma iddia yok)."
        elif citations and (not require_locator or has_locator):
            status = "CITED"
            reason = "1:1 arşiv veya e-Devlet atfı doğrulandı."
            if not has_locator:
                warnings.append(
                    f"{loc} ({subject}): Fon kodu mevcut ancak defter/kutu/gömlek/varak/poz yer bilgisi eksik."
                )
        else:
            status = "UNCITED"
            if citations and require_locator and not has_locator:
                reason = "Fon kodu var ancak zorunlu kutu/dosya/gömlek/defter/varak/poz yer bilgisi eksik."
            else:
                reason = (
                    "Geçerli arşiv fonu, kutu/dosya/gömlek/varak-poz veya e-Devlet referansı yok "
                    "ve 'doğrulanmadı' etiketi bulunmuyor."
                )

        claims.append({
            "index": idx,
            "location": loc,
            "subject": subject,
            "status": status,
            "citations": citations,
            "has_locator": has_locator,
            "reason": reason,
        })

    return _finalize_citation_report("dataset", claims, warnings)


def _extract_markdown_claims(markdown_text: str) -> tuple[list[tuple[int, str]], dict[str, str]]:
    """Markdown raporundan dipnot tanımlarını ve denetlenecek iddia satırlarını (tablo, liste, düzyazı) çıkarır."""
    raw_lines = markdown_text.splitlines()
    footnotes: dict[str, str] = {}
    footnote_lines: set[int] = set()

    in_fence = False
    fence_marker = ""
    current_fn: str | None = None
    for idx, line in enumerate(raw_lines, 1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            current_fn = None
            continue
        if in_fence:
            continue
        fn_match = _MD_FOOTNOTE_DEF_RE.match(line)
        if fn_match:
            current_fn = fn_match.group(1).strip()
            footnotes[current_fn] = fn_match.group(2).strip()
            footnote_lines.add(idx)
            continue
        if current_fn is not None and (line.startswith("  ") or line.startswith("\t")) and stripped:
            footnotes[current_fn] = f"{footnotes[current_fn]} {stripped}".strip()
            footnote_lines.add(idx)
            continue
        current_fn = None

    table_and_list_claims: list[tuple[int, str]] = []
    prose_hinted_claims: list[tuple[int, str]] = []
    prose_fallback_claims: list[tuple[int, str]] = []

    in_fence = False
    fence_marker = ""
    i = 0
    total = len(raw_lines)
    while i < total:
        lineno = i + 1
        line = raw_lines[i]
        stripped = line.strip()

        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            i += 1
            continue

        if in_fence or not stripped or lineno in footnote_lines:
            i += 1
            continue

        # Başlık, yatay çizgi veya HTML yorumu atla
        if (
            stripped.startswith("#")
            or re.match(r"^(?:[-*_]\s*){3,}$", stripped)
            or (stripped.startswith("<!--") and stripped.endswith("-->"))
        ):
            i += 1
            continue

        # Markdown tablosu (başlık + ayırıcı + veri satırları)
        if "|" in stripped and i + 1 < total and _MD_TABLE_SEP_RE.match(raw_lines[i + 1]):
            i += 2  # Başlık ve ayırıcı satırları atla
            while i < total:
                row_line = raw_lines[i].strip()
                if not row_line or "|" not in row_line or _MD_TABLE_SEP_RE.match(row_line):
                    break
                table_and_list_claims.append((i + 1, row_line))
                i += 1
            continue

        if _MD_TABLE_SEP_RE.match(stripped):
            i += 1
            continue

        # Madde işareti / numaralı liste
        list_m = _MD_LIST_ITEM_RE.match(line)
        if list_m:
            body = list_m.group(1).strip()
            if body and not _MD_TOC_LINK_RE.match(body):
                table_and_list_claims.append((lineno, body))
            i += 1
            continue

        # Alıntı veya düzyazı satırı
        clean_prose = stripped.lstrip(">").strip()
        if clean_prose:
            prose_fallback_claims.append((lineno, clean_prose))
            if _MD_CLAIM_HINT_RE.search(clean_prose):
                prose_hinted_claims.append((lineno, clean_prose))
        i += 1

    extracted = table_and_list_claims + prose_hinted_claims
    if not extracted:
        extracted = prose_fallback_claims
    extracted.sort(key=lambda pair: pair[0])
    return extracted, footnotes


def _audit_markdown_report(markdown_text: str, *, require_locator: bool = False) -> dict[str, object]:
    extracted, footnotes = _extract_markdown_claims(markdown_text)
    if not extracted:
        raise ValueError("Markdown raporunda denetlenecek iddia satırı bulunamadı.")

    claims: list[dict[str, object]] = []
    warnings: list[str] = []

    for idx, (lineno, text) in enumerate(extracted, 1):
        ref_keys = _MD_FOOTNOTE_REF_RE.findall(text)
        expanded = text
        if ref_keys:
            resolved_notes = [footnotes[k] for k in ref_keys if k in footnotes]
            if resolved_notes:
                expanded = f"{text} | {' | '.join(resolved_notes)}"

        citations, has_locator = _extract_archival_citations(expanded)
        explicitly_unverified = _is_explicitly_unverified(expanded)
        subject = re.sub(r"\s+", " ", text).strip()
        if len(subject) > 100:
            subject = subject[:97] + "..."
        loc = f"line:{lineno}"

        if explicitly_unverified:
            status = "EXPLICITLY_UNVERIFIED"
            reason = "Açıkça 'doğrulanmadı' olarak işaretlenmiş (uydurma iddia yok)."
        elif citations and (not require_locator or has_locator):
            status = "CITED"
            reason = "1:1 arşiv veya e-Devlet atfı doğrulandı."
            if not has_locator:
                warnings.append(
                    f"{loc}: Fon kodu mevcut ancak defter/kutu/gömlek/varak/poz yer bilgisi eksik."
                )
        else:
            status = "UNCITED"
            if citations and require_locator and not has_locator:
                reason = "Fon kodu var ancak zorunlu kutu/dosya/gömlek/defter/varak/poz yer bilgisi eksik."
            else:
                reason = (
                    "Geçerli arşiv fonu, kutu/dosya/gömlek/varak-poz veya e-Devlet referansı yok "
                    "ve 'doğrulanmadı' etiketi bulunmuyor."
                )

        claims.append({
            "index": idx,
            "location": loc,
            "subject": subject,
            "status": status,
            "citations": citations,
            "has_locator": has_locator,
            "reason": reason,
        })

    return _finalize_citation_report("markdown", claims, warnings)


def _finalize_citation_report(
    input_format: str,
    claims: list[dict[str, object]],
    warnings: list[str],
) -> dict[str, object]:
    total = len(claims)
    cited = sum(1 for c in claims if c["status"] == "CITED")
    explicitly_unverified = sum(1 for c in claims if c["status"] == "EXPLICITLY_UNVERIFIED")
    uncited_claims = [c for c in claims if c["status"] == "UNCITED"]
    uncited = len(uncited_claims)
    compliant = total > 0 and uncited == 0

    return {
        "compliant": compliant,
        "input_format": input_format,
        "summary": {
            "total_claims": total,
            "cited": cited,
            "explicitly_unverified": explicitly_unverified,
            "uncited": uncited,
            "coverage_ratio": round((cited + explicitly_unverified) / total, 4) if total else 0.0,
            "strict_cited_ratio": round(cited / total, 4) if total else 0.0,
        },
        "claims": claims,
        "uncited_claims": uncited_claims,
        "warnings": warnings,
    }


def verify_citations(
    source: list[dict[str, object]] | dict[str, object] | str,
    *,
    input_format: str = "auto",
    require_locator: bool = False,
) -> dict[str, object]:
    """
    Bir soy kütüğü/göç veri setini (JSON liste/sözlük) veya Markdown raporunu
    1:1 arşiv atıf kapsamı açısından deterministik olarak denetler.
    Her iddia ya geçerli bir arşiv fonu/kutu-dosya-gömlek/varak-poz/e-Devlet
    referansı taşımalı ya da açıkça `doğrulanmadı` olarak işaretlenmelidir.
    """
    fmt = (input_format or "auto").strip().lower()
    if fmt not in {"auto", "json", "dataset", "markdown"}:
        raise ValueError(f"Desteklenmeyen girdi biçimi: {input_format}")

    if isinstance(source, list):
        return _audit_dataset_records(source, require_locator=require_locator)

    if isinstance(source, dict):
        for list_key in ("records", "claims", "entries", "lineage", "timeline", "ancestors"):
            if isinstance(source.get(list_key), list):
                return _audit_dataset_records(source[list_key], require_locator=require_locator)
        return _audit_dataset_records([source], require_locator=require_locator)

    if isinstance(source, str):
        stripped = source.strip()
        if not stripped:
            raise ValueError("Denetlenecek içerik boş olamaz.")
        if fmt in {"json", "dataset"} or (fmt == "auto" and (stripped.startswith("[") or stripped.startswith("{"))):
            try:
                parsed = json.loads(stripped)
            except Exception as exc:
                if fmt in {"json", "dataset"}:
                    raise ValueError(f"Geçersiz JSON girdisi: {exc}") from exc
                return _audit_markdown_report(source, require_locator=require_locator)
            return verify_citations(parsed, input_format="dataset", require_locator=require_locator)
        return _audit_markdown_report(source, require_locator=require_locator)

    raise ValueError("verify_citations girdisi liste, sözlük veya metin olmalıdır.")


def format_citation_report_markdown(report: dict[str, object]) -> str:
    """`verify_citations` çıktısını yapılandırılmış Markdown denetim raporuna dönüştürür."""
    summary = report["summary"]
    verdict = "UYUMLU (0 atıfsız iddia)" if report["compliant"] else f"UYUMSUZ ({summary['uncited']} atıfsız iddia)"
    lines = [
        "### 1:1 Arşiv Atıf Doğrulama Raporu (`verify-citations`)",
        "",
        f"- **Durum:** **{verdict}**",
        f"- **Girdi Biçimi:** `{report['input_format']}`",
        f"- **Toplam İddia:** {summary['total_claims']} "
        f"(Atıflı: {summary['cited']} · Açıkça `doğrulanmadı`: {summary['explicitly_unverified']} · "
        f"Atıfsız/İhlal: {summary['uncited']})",
        f"- **Uyumluluk Oranı:** %{summary['coverage_ratio'] * 100:.1f} "
        f"(Doğrudan Atıf Oranı: %{summary['strict_cited_ratio'] * 100:.1f})",
        "",
        "| # | Konum | Özne / Satır | Durum | Tespit Edilen Atıflar | Açıklama |",
        "|---|-------|--------------|-------|-----------------------|----------|",
    ]
    for claim in report["claims"]:
        cits = ", ".join(sanitize_markdown_cell(c) for c in claim["citations"]) or "—"
        lines.append(
            f"| {claim['index']} | {sanitize_markdown_cell(claim['location'])} | "
            f"{sanitize_markdown_cell(claim['subject']) or '—'} | `{claim['status']}` | "
            f"{cits} | {sanitize_markdown_cell(claim['reason'])} |"
        )
    if report.get("warnings"):
        lines.extend(["", "#### Uyarılar"])
        for w in report["warnings"]:
            lines.append(f"- {sanitize_markdown_cell(w)}")
    return "\n".join(lines)


def _load_citation_source_arg(raw_arg: str, *, explicit_format: str = "auto") -> tuple[object, str]:
    """Inline JSON, JSON dosya yolu veya Markdown rapor dosyasını boyut/yol sınırlarıyla yükler."""
    raw = (raw_arg or "").strip()
    if not raw:
        raise ValueError("Girdi parametresi boş olamaz.")
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError(f"Girdi izin verilen boyutu ({MAX_JSON_BYTES} B) aşıyor.")

    if explicit_format in {"auto", "json", "dataset"} and (raw.startswith("[") or raw.startswith("{")):
        try:
            return json.loads(raw), "dataset"
        except Exception as exc:
            if explicit_format in {"json", "dataset"}:
                raise ValueError(f"Geçersiz JSON girdisi: {exc}") from exc

    safe_path = validate_safe_path(raw)
    if not safe_path.is_file():
        raise ValueError(f"Girdi dosyası bulunamadı: {raw}")
    if safe_path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"Girdi dosyası izin verilen boyutu ({MAX_JSON_BYTES} B) aşıyor.")
    text = safe_path.read_text(encoding="utf-8")
    if explicit_format != "auto":
        return text, explicit_format
    if safe_path.suffix.lower() == ".json":
        return text, "json"
    if safe_path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return text, "markdown"
    return text, "auto"


def cmd_verify_citations(args: argparse.Namespace) -> None:
    raw_input = args.input or args.records_json or args.markdown
    explicit_fmt = "auto"
    if args.records_json:
        explicit_fmt = "json"
    elif args.markdown:
        explicit_fmt = "markdown"

    try:
        source, detected_fmt = _load_citation_source_arg(raw_input, explicit_format=explicit_fmt)
        report = verify_citations(
            source,
            input_format=detected_fmt,
            require_locator=bool(args.require_locator),
        )
    except ValueError as exc:
        sys.exit(f"Hata: {exc}")

    if args.format == "markdown":
        print(format_citation_report_markdown(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if not report["compliant"]:
        sys.exit(1)


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

    p_vc = sub.add_parser(
        "verify-citations",
        help="Soy kütüğü veri setini (JSON) veya Markdown raporunu 1:1 arşiv atfı / 'doğrulanmadı' uyumluluğu açısından denetler.",
    )
    vc_src = p_vc.add_mutually_exclusive_group(required=True)
    vc_src.add_argument("--input", help="JSON veri seti (inline veya dosya yolu) veya Markdown rapor dosyası")
    vc_src.add_argument("--records-json", help="JSON dizisi veya JSON dosya yolu")
    vc_src.add_argument("--markdown", help="Denetlenecek Markdown rapor dosya yolu")
    p_vc.add_argument("--format", choices=["json", "markdown"], default="json", help="Çıktı rapor biçimi (varsayılan: json)")
    p_vc.add_argument(
        "--require-locator",
        action="store_true",
        help="Sadece fon kodu yerine defter/kutu/dosya/gömlek/varak-poz veya e-Devlet yer bilgisini de zorunlu tut",
    )
    p_vc.set_defaults(func=cmd_verify_citations)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
