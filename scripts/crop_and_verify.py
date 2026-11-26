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
import os
import sys
import zipfile
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("Hata: 'Pillow' kütüphanesi kurulu değil. Kurmak için: pip install Pillow")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}


def extract_zips_if_needed(input_dir: Path, extract_root: Path) -> list[Path]:
    """Klasördeki .zip arşivlerini güvenli biçimde açar ve tüm görüntü yollarını döndürür."""
    images: list[Path] = []
    for root, _, files in os.walk(input_dir):
        for fn in sorted(files):
            fpath = Path(root) / fn
            ext = fpath.suffix.lower()
            if ext == ".zip":
                target = extract_root / fpath.stem
                if not target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(fpath, "r") as zf:
                        target_resolved = target.resolve()
                        for member in zf.infolist():
                            # Zip-slip güvenlik kontrolü
                            dest = (target / member.filename).resolve()
                            if not dest.is_relative_to(target_resolved):
                                raise ValueError(f"Güvensiz zip yolu reddedildi: {member.filename}")
                            zf.extract(member, target)
                for zroot, _, zfiles in os.walk(target):
                    for zfn in sorted(zfiles):
                        zp = Path(zroot) / zfn
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
    in_dir = Path(args.input)
    if not in_dir.exists():
        sys.exit(f"Hata: Girdi klasörü bulunamadı: {in_dir}")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    imgs = extract_zips_if_needed(in_dir, out_dir / "_extracted")
    print(f"Toplam Görüntü Sayısı: {len(imgs)}")
    for idx, p in enumerate(imgs, 1):
        with Image.open(p) as im:
            w, h = im.size
        print(f"[{idx:03d}] {p.name}  ({w}x{h} px)  ->  {p}")


def cmd_split(args: argparse.Namespace) -> None:
    img_path = Path(args.image)
    if not img_path.is_file():
        sys.exit(f"Hata: Görüntü dosyası bulunamadı: {img_path}")
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(img_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        stem = img_path.stem

        # Osmanlı defterlerinde sağ sayfa önce (a yüzü), sol sayfa sonra (b yüzü) okunur.
        halves = [
            ("R", im.crop((int(w * 0.47), 0, w, h))),
            ("L", im.crop((0, 0, int(w * 0.53), h))),
        ]
        for side, sub in halves:
            sw, sh = sub.size
            full_side = out_dir / f"{stem}_{side}_full.jpg"
            sub.save(full_side, quality=92)
            print(f"Kaydedildi: {full_side} ({sw}x{sh})")

            if args.bands:
                bands = [
                    ("1_top", (0, 0, sw, int(sh * 0.37))),
                    ("2_mid", (0, int(sh * 0.31), sw, int(sh * 0.69))),
                    ("3_bot", (0, int(sh * 0.63), sw, sh)),
                ]
                for bname, box in bands:
                    bp = out_dir / f"{stem}_{side}_{bname}.png"
                    sub.crop(box).save(bp)
                    print(f"  Şerit: {bp}")


def cmd_crop(args: argparse.Namespace) -> None:
    img_path = Path(args.image)
    if not img_path.is_file():
        sys.exit(f"Hata: Görüntü dosyası bulunamadı: {img_path}")
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        vals = [float(v.strip()) for v in args.box.split(",")]
    except ValueError:
        sys.exit("Hata: --box parametresi sayısal değerlerden oluşmalıdır ('x1,y1,x2,y2').")
    if len(vals) != 4:
        sys.exit("Hata: --box parametresi 'x1,y1,x2,y2' formatında 4 sayı olmalıdır (0.0-1.0 oran veya piksel).")

    with Image.open(img_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if all(0.0 <= v <= 1.0 for v in vals):
            x1, y1, x2, y2 = int(vals[0] * w), int(vals[1] * h), int(vals[2] * w), int(vals[3] * h)
        else:
            x1, y1, x2, y2 = [int(v) for v in vals]

        x1, x2 = max(0, min(w, x1)), max(0, min(w, x2))
        y1, y2 = max(0, min(h, y1)), max(0, min(h, y2))
        if x2 <= x1 or y2 <= y1:
            sys.exit(f"Hata: Geçersiz kesit koordinatları ({x1},{y1},{x2},{y2}); x2 > x1 ve y2 > y1 olmalıdır.")

        crop = im.crop((x1, y1, x2, y2))
        if args.zoom and args.zoom > 1:
            resample = Image.Resampling.NEAREST if args.nearest else Image.Resampling.LANCZOS
            crop = crop.resize((int(crop.width * args.zoom), int(crop.height * args.zoom)), resample)

        crop.save(out_path)
        print(f"1:1 Kanıt Kesiti Kaydedildi: {out_path} ({crop.width}x{crop.height} px, kaynak kutu=({x1},{y1},{x2},{y2}))")


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

    for item in enriched:
        nid = str(item["node_id"])
        hane = str(item["hane"])
        isim = str(item["isim"]).replace('"', "'")
        unvan = str(item["unvan"]).replace('"', "'")
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
                safe_baba = baba.replace('"', "'")
                lines.append(f'    {parent_id}["Hane {hane}: {safe_baba} (Ata / Baba)"]')
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


def cmd_lineage_tree(args: argparse.Namespace) -> None:
    raw = (args.records_json or "").strip()
    if not raw:
        sys.exit("Hata: --records-json parametresi boş olamaz.")
    candidate_path = Path(raw)
    if candidate_path.is_file():
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception as exc:
            sys.exit(f"Hata: JSON dosyası okunamadı: {exc}")
    else:
        try:
            payload = json.loads(raw)
        except Exception as exc:
            sys.exit(f"Hata: Geçersiz JSON girdisi: {exc}")

    try:
        res = build_lineage_mermaid(payload)
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()



