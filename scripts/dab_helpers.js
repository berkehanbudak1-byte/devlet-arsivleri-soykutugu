// T.C. Cumhurbaşkanlığı Devlet Arşivleri Başkanlığı Kataloğu (katalog.devletarsivleri.gov.tr)
// Chrome DevTools MCP evaluate_script yardımcı kütüphanesi (Salt-okunur / WAF & Hız Sınırı Korumalı)
(() => {
  const rootObj = typeof window !== 'undefined' ? window : globalThis;
  if (rootObj.__DAB) return;

  const ALLOWED_PROTOCOLS = new Set(['http:', 'https:']);
  const ALLOWED_MODES = new Set(['full', 'right', 'left']);
  const MAX_BATCH_TERMS = 50;
  const MAX_CANVAS_DIM = 4096;
  const MAX_MI_INDEX = 100000;
  const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;
  const DANGEROUS_JSON_KEYS = new Set(['__proto__', 'constructor', 'prototype']);

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function getBaseHref() {
    if (typeof window !== 'undefined' && window.location && window.location.href) {
      return window.location.href;
    }
    return 'https://katalog.devletarsivleri.gov.tr/';
  }

  /**
   * URL'nin güvenli (http/https, kontrol karaktersiz ve aynı kaynak / same-origin) olduğunu doğrular.
   * javascript:, data:, file:, vbscript: gibi şemaları ve farklı origin yönlendirmelerini engeller.
   */
  function assertSafeUrl(rawUrl, baseHref = getBaseHref()) {
    if (typeof rawUrl !== 'string' || !rawUrl.trim()) {
      throw new Error('Geçersiz veya boş URL.');
    }
    const trimmed = rawUrl.trim();
    if (CONTROL_CHAR_RE.test(trimmed)) {
      throw new Error('URL içinde geçersiz kontrol karakteri tespit edildi.');
    }
    let parsed;
    let baseParsed;
    try {
      baseParsed = new URL(baseHref);
      parsed = new URL(trimmed, baseParsed);
    } catch {
      throw new Error('URL ayrıştırılamadı.');
    }
    if (!ALLOWED_PROTOCOLS.has(parsed.protocol)) {
      throw new Error(`Güvensiz URL protokolü reddedildi: ${parsed.protocol}`);
    }
    if (parsed.origin !== baseParsed.origin) {
      throw new Error(`Farklı kaynak (cross-origin) URL reddedildi: ${parsed.origin}`);
    }
    return parsed.href;
  }

  /**
   * DOM bağlantısından (href) yalnızca güvenli aynı-kaynak http/https URL döndürür;
   * javascript: veya harici bağlantıları boş dizge olarak temizler.
   */
  function sanitizeHref(rawHref, baseHref = getBaseHref()) {
    if (typeof rawHref !== 'string' || !rawHref.trim()) return '';
    try {
      return assertSafeUrl(rawHref, baseHref);
    } catch {
      return '';
    }
  }

  /**
   * Prototip kirletme (__proto__, constructor, prototype) anahtarlarını ayıklayan güvenli JSON ayrıştırıcı.
   */
  function safeJsonParse(rawText) {
    if (typeof rawText !== 'string') {
      throw new Error('JSON girdisi metin (string) olmalıdır.');
    }
    return JSON.parse(rawText, (key, value) => {
      if (DANGEROUS_JSON_KEYS.has(key)) return undefined;
      return value;
    });
  }

  function assertSafeResponse(resp, htmlText = '') {
    if (resp.status === 418 || htmlText.includes('Erişim Engellendi')) {
      throw new Error(
        'UYARI (HTTP 418 — Hız Sınırı): Devlet Arşivleri güvenlik duvarı çok sık istek nedeniyle geçici engel uyguladı. ' +
        'Otomatik istekleri DERHAL durdurun; kullanıcıya defter ve poz (Mi) numaralarını vererek tarayıcıdan manuel açmasını isteyin.'
      );
    }
    if (
      resp.redirected &&
      typeof resp.url === 'string' &&
      (resp.url.includes('Giris') || resp.url.includes('giris.turkiye.gov.tr'))
    ) {
      throw new Error(
        'Oturum süresi doldu veya e-Devlet giriş ekranına yönlendirildi. Lütfen kullanıcının tarayıcıda bizzat giriş yapmasını bekleyin.'
      );
    }
  }

  /**
   * Basit Arama (Arama.aspx) sayfasında sıralı ve gecikmeli anahtar kelime taraması yapar.
   * WAF (HTTP 418) tetiklememek için istekler asla paralel atılmaz; varsayılan bekleme 1800 ms'dir.
   */
  async function batchSearch(terms, delayMs = 1800) {
    if (!Array.isArray(terms) || terms.length === 0) {
      throw new Error('Arama terimleri (terms) boş olmayan bir dizi olmalıdır.');
    }
    if (terms.length > MAX_BATCH_TERMS) {
      throw new Error(`Tek seferde en fazla ${MAX_BATCH_TERMS} arama terimi işlenebilir.`);
    }
    const safeDelay = Number.isFinite(delayMs) && delayMs >= 0 ? Math.min(delayMs, 60000) : 1800;

    const form = document.querySelector('form');
    if (!form) {
      throw new Error('Arama formu bulunamadı. Lütfen Arama.aspx sayfasında olduğunuzdan emin olun.');
    }
    const pageUrl = assertSafeUrl(window.location.href);
    const baseData = new FormData(form);
    const results = [];

    for (let i = 0; i < terms.length; i++) {
      const term = String(terms[i] ?? '').trim();
      if (!term || CONTROL_CHAR_RE.test(term)) {
        throw new Error('Arama terimi boş olamaz ve kontrol karakteri içeremez.');
      }
      if (i > 0 && safeDelay > 0) await sleep(safeDelay);

      const fd = new URLSearchParams();
      for (const [k, v] of baseData.entries()) {
        if (k === 'ctl00$Content$txtArama') fd.set(k, term);
        else if (!k.includes('btn')) fd.set(k, String(v));
      }
      fd.set('ctl00$Content$txtArama', term);
      fd.set('ctl00$Content$btnAra', 'Ara');

      const resp = await fetch(pageUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: fd.toString()
      });
      const html = await resp.text();
      assertSafeResponse(resp, html);

      const doc = new DOMParser().parseFromString(html, 'text/html');
      const bodyText = doc.body?.textContent || '';
      const totalMatch = bodyText.match(/Bulunan Kayıt Sayısı\s*:\s*(\d+)/);
      const rows = [...doc.querySelectorAll('table tr')].slice(1).map((tr) => {
        const tds = [...tr.querySelectorAll('td')]
          .map((td) => (td.textContent || '').trim())
          .filter(Boolean);
        const rawHref = tr.querySelector('a[href*="BelgeGoster"]')?.getAttribute('href') || '';
        const link = sanitizeHref(rawHref, pageUrl);
        return { tds, link };
      });
      results.push({
        term,
        total: totalMatch ? Number(totalMatch[1]) : rows.length,
        returnedRows: rows.length,
        rows
      });
    }
    return results;
  }

  /**
   * Bir defterin/belgenin toplam görüntü (poz) sayısını kontrol eder.
   */
  async function getRegisterMeta(belgeUrl) {
    const safeBelgeUrl = assertSafeUrl(belgeUrl);
    const resp = await fetch(safeBelgeUrl);
    const html = await resp.text();
    assertSafeResponse(resp, html);

    const doc = new DOMParser().parseFromString(html, 'text/html');
    const text = doc.body?.textContent || '';
    const imgCount = text.match(/Görüntü Sayısı\s*:\s*(\d+)/)?.[1] || null;
    const hasPicture = Boolean(doc.getElementById('sample_picture'));
    return { belgeUrl: safeBelgeUrl, imgCount: imgCount ? Number(imgCount) : null, hasPicture };
  }

  /**
   * BelgeGoster.aspx üzerindeki çift varaklı defter görüntüsünü ('full' | 'right' | 'left')
   * yüksek çözünürlüklü <canvas> olarak hazırlar ve isteğe bağlı JPEG DataURL döndürür.
   * Not: Aynı kaynak (same-origin) olduğundan img.crossOrigin ayarlanmaz.
   */
  async function renderSpreadPage(belgeUrl, mi = 0, mode = 'full', maxWidth = 1600, returnDataUrl = false) {
    const safeBaseUrl = assertSafeUrl(belgeUrl);
    if (!Number.isInteger(mi) || mi < 0 || mi > MAX_MI_INDEX) {
      throw new Error(`Geçersiz poz indeksi (Mi): 0-${MAX_MI_INDEX} arasında tam sayı olmalıdır.`);
    }
    if (!ALLOWED_MODES.has(mode)) {
      throw new Error(`Geçersiz görünüm modu: ${mode} ('full', 'right' veya 'left' olmalıdır).`);
    }
    const safeMaxWidth = Number.isFinite(maxWidth)
      ? Math.max(64, Math.min(MAX_CANVAS_DIM, Math.round(maxWidth)))
      : 1600;

    const targetUrlObj = new URL(safeBaseUrl);
    targetUrlObj.searchParams.set('Mi', String(mi));
    const targetUrl = assertSafeUrl(targetUrlObj.href);

    const resp = await fetch(targetUrl);
    const html = await resp.text();
    assertSafeResponse(resp, html);

    const doc = new DOMParser().parseFromString(html, 'text/html');
    const rawSrc = doc.getElementById('sample_picture')?.getAttribute('src');
    if (!rawSrc) {
      throw new Error('Belge görüntüsü (#sample_picture) bulunamadı veya oturum doğrulaması gerekiyor.');
    }
    const src = assertSafeUrl(rawSrc, targetUrl);

    const img = new Image();
    img.src = src;
    await img.decode();
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
      throw new Error('Geçersiz görüntü boyutları.');
    }

    let sx = 0, sy = 0, sw = w, sh = h;
    if (mode === 'right') {
      sx = Math.floor(w * 0.48);
      sw = w - sx;
    } else if (mode === 'left') {
      sx = 0;
      sw = Math.floor(w * 0.52);
    }

    const scale = Math.min(1, safeMaxWidth / sw, MAX_CANVAS_DIM / sh);
    const c = document.createElement('canvas');
    c.id = '__dab_canvas';
    c.width = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(sw * scale)));
    c.height = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(sh * scale)));
    c.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, c.width, c.height);

    rootObj.__DAB_LAST_IMG = img;
    const out = {
      targetUrl,
      naturalWidth: w,
      naturalHeight: h,
      canvasWidth: c.width,
      canvasHeight: c.height,
      mode
    };
    if (returnDataUrl) {
      out.dataUrl = c.toDataURL('image/jpeg', 0.88);
    }
    return out;
  }

  /**
   * Son yüklenen defter görüntüsünden 1:1 doğal piksel çözünürlüğünde (veya büyütülmüş)
   * bölgesel kesit (crop) alır. Oranlar 0.0 - 1.0 aralığındadır.
   */
  function cropLastImageDataUrl(xRatio, yRatio, wRatio, hRatio, zoom = 1, quality = 0.92) {
    const img = rootObj.__DAB_LAST_IMG;
    if (!img) throw new Error('Önce renderSpreadPage çağrılmalıdır.');
    for (const [label, val] of [['xRatio', xRatio], ['yRatio', yRatio], ['wRatio', wRatio], ['hRatio', hRatio]]) {
      if (typeof val !== 'number' || !Number.isFinite(val) || val < 0 || val > 1) {
        throw new Error(`${label} parametresi 0.0 ile 1.0 arasında sonlu bir sayı olmalıdır.`);
      }
    }
    if (wRatio <= 0 || hRatio <= 0) {
      throw new Error('wRatio ve hRatio pozitif olmalıdır.');
    }
    if (typeof zoom !== 'number' || !Number.isFinite(zoom) || zoom < 0.1 || zoom > 4) {
      throw new Error('zoom parametresi 0.1 ile 4.0 arasında olmalıdır.');
    }
    const safeQuality = typeof quality === 'number' && Number.isFinite(quality)
      ? Math.max(0.1, Math.min(1.0, quality))
      : 0.92;

    const sx = Math.max(0, Math.round(img.naturalWidth * xRatio));
    const sy = Math.max(0, Math.round(img.naturalHeight * yRatio));
    const sw = Math.min(img.naturalWidth - sx, Math.round(img.naturalWidth * wRatio));
    const sh = Math.min(img.naturalHeight - sy, Math.round(img.naturalHeight * hRatio));
    if (sw <= 0 || sh <= 0) {
      throw new Error('Geçersiz kesit alanı: genişlik ve yükseklik pozitif olmalıdır.');
    }

    const outWidth = Math.round(sw * zoom);
    const outHeight = Math.round(sh * zoom);
    if (outWidth <= 0 || outHeight <= 0 || outWidth > MAX_CANVAS_DIM || outHeight > MAX_CANVAS_DIM) {
      throw new Error(`Kesit çıktı boyutu (${outWidth}x${outHeight}) güvenli sınırı (${MAX_CANVAS_DIM}px) aşıyor.`);
    }

    const c = document.createElement('canvas');
    c.width = outWidth;
    c.height = outHeight;
    const ctx = c.getContext('2d');
    ctx.imageSmoothingEnabled = zoom <= 1.5;
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, c.width, c.height);
    return {
      sx, sy, sw, sh,
      outWidth: c.width,
      outHeight: c.height,
      dataUrl: c.toDataURL('image/jpeg', safeQuality)
    };
  }

  const api = {
    assertSafeUrl,
    sanitizeHref,
    safeJsonParse,
    batchSearch,
    getRegisterMeta,
    renderSpreadPage,
    cropLastImageDataUrl
  };
  rootObj.__DAB = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})();
