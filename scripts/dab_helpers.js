// T.C. Cumhurbaşkanlığı Devlet Arşivleri Başkanlığı Kataloğu (katalog.devletarsivleri.gov.tr)
// Chrome DevTools MCP evaluate_script yardımcı kütüphanesi (Salt-okunur / WAF & Hız Sınırı Korumalı)
(() => {
  if (window.__DAB) return;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function assertSafeResponse(resp, htmlText = '') {
    if (resp.status === 418 || htmlText.includes('Erişim Engellendi')) {
      throw new Error(
        'UYARI (HTTP 418 — Hız Sınırı): Devlet Arşivleri güvenlik duvarı çok sık istek nedeniyle geçici engel uyguladı. ' +
        'Otomatik istekleri DERHAL durdurun; kullanıcıya defter ve poz (Mi) numaralarını vererek tarayıcıdan manuel açmasını isteyin.'
      );
    }
    if (
      resp.redirected &&
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
    const form = document.querySelector('form');
    if (!form) {
      throw new Error('Arama formu bulunamadı. Lütfen Arama.aspx sayfasında olduğunuzdan emin olun.');
    }
    const baseData = new FormData(form);
    const results = [];

    for (let i = 0; i < terms.length; i++) {
      const term = terms[i];
      if (i > 0 && delayMs > 0) await sleep(delayMs);

      const fd = new URLSearchParams();
      for (const [k, v] of baseData.entries()) {
        if (k === 'ctl00$Content$txtArama') fd.set(k, term);
        else if (!k.includes('btn')) fd.set(k, v);
      }
      fd.set('ctl00$Content$txtArama', term);
      fd.set('ctl00$Content$btnAra', 'Ara');

      const resp = await fetch(window.location.href, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: fd.toString()
      });
      const html = await resp.text();
      assertSafeResponse(resp, html);

      const doc = new DOMParser().parseFromString(html, 'text/html');
      const totalMatch = doc.body.innerText.match(/Bulunan Kayıt Sayısı\s*:\s*(\d+)/);
      const rows = [...doc.querySelectorAll('table tr')].slice(1).map((tr) => {
        const tds = [...tr.querySelectorAll('td')].map((td) => td.innerText.trim()).filter(Boolean);
        const link = tr.querySelector('a[href*="BelgeGoster"]')?.href || '';
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
    const resp = await fetch(belgeUrl);
    const html = await resp.text();
    assertSafeResponse(resp, html);

    const doc = new DOMParser().parseFromString(html, 'text/html');
    const text = doc.body.innerText;
    const imgCount = text.match(/Görüntü Sayısı\s*:\s*(\d+)/)?.[1] || null;
    const hasPicture = Boolean(doc.getElementById('sample_picture'));
    return { belgeUrl, imgCount: imgCount ? Number(imgCount) : null, hasPicture };
  }

  /**
   * BelgeGoster.aspx üzerindeki çift varaklı defter görüntüsünü ('full' | 'right' | 'left')
   * yüksek çözünürlüklü <canvas> olarak hazırlar ve isteğe bağlı JPEG DataURL döndürür.
   * Not: Aynı kaynak (same-origin) olduğundan img.crossOrigin ayarlanmaz.
   */
  async function renderSpreadPage(belgeUrl, mi = 0, mode = 'full', maxWidth = 1600, returnDataUrl = false) {
    const targetUrl = belgeUrl.replace(/([?&])Mi=\d+/, '$1Mi=' + mi);
    const resp = await fetch(targetUrl);
    const html = await resp.text();
    assertSafeResponse(resp, html);

    const doc = new DOMParser().parseFromString(html, 'text/html');
    const src = doc.getElementById('sample_picture')?.src;
    if (!src) {
      throw new Error('Belge görüntüsü (#sample_picture) bulunamadı veya oturum doğrulaması gerekiyor.');
    }

    const img = new Image();
    img.src = src;
    await img.decode();
    const w = img.naturalWidth;
    const h = img.naturalHeight;

    let sx = 0, sy = 0, sw = w, sh = h;
    if (mode === 'right') {
      sx = Math.floor(w * 0.48);
      sw = w - sx;
    } else if (mode === 'left') {
      sx = 0;
      sw = Math.floor(w * 0.52);
    }

    const scale = Math.min(1, maxWidth / sw);
    const c = document.createElement('canvas');
    c.id = '__dab_canvas';
    c.width = Math.round(sw * scale);
    c.height = Math.round(sh * scale);
    c.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, c.width, c.height);

    window.__DAB_LAST_IMG = img;
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
    const img = window.__DAB_LAST_IMG;
    if (!img) throw new Error('Önce renderSpreadPage çağrılmalıdır.');
    const sx = Math.max(0, Math.round(img.naturalWidth * xRatio));
    const sy = Math.max(0, Math.round(img.naturalHeight * yRatio));
    const sw = Math.min(img.naturalWidth - sx, Math.round(img.naturalWidth * wRatio));
    const sh = Math.min(img.naturalHeight - sy, Math.round(img.naturalHeight * hRatio));

    const c = document.createElement('canvas');
    c.width = Math.round(sw * zoom);
    c.height = Math.round(sh * zoom);
    const ctx = c.getContext('2d');
    ctx.imageSmoothingEnabled = zoom <= 1.5;
    ctx.drawImage(img, sx, sy, sw, sh, 0, 0, c.width, c.height);
    return {
      sx, sy, sw, sh,
      outWidth: c.width,
      outHeight: c.height,
      dataUrl: c.toDataURL('image/jpeg', quality)
    };
  }

  window.__DAB = { batchSearch, getRegisterMeta, renderSpreadPage, cropLastImageDataUrl };
})();
