// ==UserScript==
// @name         MuseAI Video Studio - Trợ Lý Nhập Cookie
// @namespace    https://github.com/czg86389-hub/muse2api
// @version      1.1.0
// @description  Trích xuất và đồng bộ Cookie đăng nhập từ muse.ai vào ứng dụng MuseAI Video Studio chỉ với 1 click chuột, hỗ trợ bảng điều khiển nổi hiện đại.
// @author       MuseAI Video Studio
// @match        https://muse.ai/*
// @match        https://*.muse.ai/*
// @grant        GM_cookie
// @grant        GM_setClipboard
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      *
// @run-at       document-end
// ==/UserScript==

(function () {
  'use strict';

  const STORE_KEY = 'museai_cookie_cfg';
  const ESSENTIAL = ['hatch_sess', 'hatch_gw', 'hatch_vml', 'hatch_native_auth_device'];

  function loadCfg() {
    const fallback = { base: 'http://127.0.0.1:18610', key: '', label: '' };
    if (typeof GM_getValue === 'function') {
      return GM_getValue(STORE_KEY, fallback);
    }
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY)) || fallback;
    } catch (_) {
      return fallback;
    }
  }

  function saveCfg(cfg) {
    if (typeof GM_setValue === 'function') {
      GM_setValue(STORE_KEY, cfg);
    } else {
      localStorage.setItem(STORE_KEY, JSON.stringify(cfg));
    }
  }

  function normBase(v) {
    let s = (v || '').trim();
    if (!s) return 'http://127.0.0.1:18610';
    if (!/^https?:\/\//i.test(s)) {
      s = (/^(127\.|100\.|10\.|192\.168\.|localhost)/i.test(s) ? 'http://' : 'https://') + s;
    }
    s = s.replace(/\/+$/, '').replace(/\/v1$/i, '');
    return s;
  }

  function showToast(msg, type = 'info') {
    const el = document.createElement('div');
    el.textContent = msg;
    const bg = type === 'ok' ? '#059669' : type === 'err' ? '#dc2626' : '#2563eb';
    el.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:9999999;padding:10px 18px;background:${bg};color:#fff;border-radius:10px;font-size:13px;font-weight:500;font-family:-apple-system,BlinkMacSystemFont,sans-serif;box-shadow:0 12px 30px rgba(0,0,0,0.5);transition:opacity 0.3s;max-width:420px;text-align:center;line-height:1.5;pointer-events:none;`;
    document.body.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 300);
    }, 4000);
  }

  async function getCookies() {
    const out = {}, exp = {};
    if (typeof GM_cookie !== 'undefined' && typeof GM_cookie.list === 'function') {
      try {
        const queries = [
          { url: 'https://muse.ai/' },
          { domain: '.muse.ai' },
          { domain: 'muse.ai' },
        ];
        const all = [];
        for (const q of queries) {
          try {
            const list = await new Promise((resolve) => {
              GM_cookie.list(q, (cookies, error) => {
                resolve(error ? [] : (cookies || []));
              });
            });
            if (Array.isArray(list)) all.push(...list);
          } catch (_) {}
        }

        for (const c of all) {
          if (!c || !c.name) continue;
          const dom = (c.domain || '').replace(/^\./, '');
          if (!dom.endsWith('muse.ai')) continue;
          out[c.name] = c.value;
          if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
        }
      } catch (_) {}
    }

    try {
      const parts = document.cookie.split(';');
      for (const p of parts) {
        const idx = p.indexOf('=');
        if (idx > 0) {
          const k = p.slice(0, idx).trim();
          const v = p.slice(idx + 1).trim();
          if (k && !(k in out)) out[k] = v;
        }
      }
    } catch (_) {}

    return { cookies: out, expires: exp };
  }

  async function checkSessionStatus() {
    const { cookies } = await getCookies();
    const hasCore = !!cookies['hatch_sess'];
    const missing = hasCore ? [] : ['hatch_sess'];
    return { hasCore, count: Object.keys(cookies).length, missing, cookies };
  }

  async function executePush(btnEl = null) {
    const cfg = loadCfg();
    const base = normBase(cfg.base);
    const key = (cfg.key || '').trim();
    const label = (cfg.label || '').trim();

    if (btnEl) {
      btnEl.disabled = true;
      btnEl.innerHTML = '<span style="opacity:0.8">⏳ Đang đọc Cookie…</span>';
    }

    try {
      const { cookies, expires } = await getCookies();
      const names = Object.keys(cookies);

      if (!names.length || !cookies['hatch_sess']) {
        showToast('Chưa phát hiện Cookie đăng nhập (hatch_sess), vui lòng đăng nhập trước!', 'err');
        return;
      }

      const cookieStr = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
      if (typeof GM_setClipboard === 'function') {
        GM_setClipboard(cookieStr);
      }

      if (!base) {
        showToast('✓ Đã sao chép Cookie vào Clipboard! Hãy nhập địa chỉ máy chủ để đồng bộ tự động.', 'ok');
        return;
      }

      if (btnEl) btnEl.innerHTML = '<span style="opacity:0.8">🚀 Đang gửi tới Studio…</span>';

      const headers = { 'Content-Type': 'application/json' };
      if (key) {
        headers['Authorization'] = 'Bearer ' + key;
      }

      const payload = JSON.stringify({ label, cookies, expires });

      const doFetch = () => {
        if (typeof GM_xmlhttpRequest === 'function') {
          return new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
              method: 'POST',
              url: base + '/admin/accounts',
              headers,
              data: payload,
              onload: (res) => resolve(res),
              onerror: (err) => reject(err),
              ontimeout: () => reject(new Error('Hết thời gian chờ kết nối máy chủ')),
            });
          });
        }
        return fetch(base + '/admin/accounts', { method: 'POST', headers, body: payload })
          .then(async (r) => ({ status: r.status, responseText: await r.text() }));
      };

      const res = await doFetch();

      if (res.status === 200 || res.status === 201) {
        let respData = {};
        try { respData = JSON.parse(res.responseText); } catch (_) {}
        const warn = respData.warning ? ` (Lưu ý: ${respData.warning})` : '';
        showToast(`✓ Đã nạp tài khoản vào MuseAI Studio thành công!${warn}`, 'ok');
        updateStatusBadge();
      } else if (res.status === 401) {
        showToast('Mã bảo mật (API Key) không đúng! Vui lòng kiểm tra lại.', 'err');
      } else {
        let errDesc = `Lỗi HTTP ${res.status}`;
        try {
          const j = JSON.parse(res.responseText);
          errDesc = j.detail || j.message || errDesc;
        } catch (_) {}
        showToast(`Đồng bộ thất bại: ${errDesc}`, 'err');
      }
    } catch (e) {
      showToast(`Lỗi kết nối: ${e.message || e}. Đã sao chép Cookie vào bộ nhớ đệm!`, 'err');
    } finally {
      if (btnEl) {
        btnEl.disabled = false;
        btnEl.innerHTML = '⚡ Đồng Bộ Vào MuseAI Studio';
      }
    }
  }

  async function updateStatusBadge() {
    const badge = document.getElementById('muse-status-tag');
    if (!badge) return;
    const st = await checkSessionStatus();
    if (st.hasCore) {
      badge.style.color = '#22c55e';
      badge.textContent = `✓ Đã sẵn sàng (${st.count} cookie)`;
    } else {
      badge.style.color = '#f59e0b';
      badge.textContent = `⚠ Chưa sẵn sàng`;
    }
  }

  function togglePanel() {
    const existing = document.getElementById('muse2api-token-panel');
    if (existing) {
      existing.remove();
      return;
    }
    showPanel();
  }

  function showPanel() {
    if (document.getElementById('muse2api-token-panel')) {
      document.getElementById('muse2api-token-panel').remove();
    }

    const cfg = loadCfg();
    const panel = document.createElement('div');
    panel.id = 'muse2api-token-panel';
    panel.innerHTML = `
      <div style="position:fixed; top:20px; right:20px; z-index:999999;
                  background:linear-gradient(180deg, rgba(15, 23, 42, 0.95) 0%, rgba(10, 15, 30, 0.96) 100%);
                  color:#f8fafc; padding:20px 22px; width:360px; max-width:calc(100vw - 40px);
                  border-radius:16px; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                  box-shadow:0 20px 50px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(96, 165, 250, 0.25);
                  backdrop-filter:blur(16px); font-size:13px; line-height:1.4;">
        
        <!-- Tiêu đề -->
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #1e293b;">
          <div style="display:flex; align-items:center; gap:8px;">
            <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:#38bdf8; box-shadow:0 0 10px #38bdf8;"></span>
            <b style="font-size:15px; color:#f1f5f9; letter-spacing:0.3px;">MuseAI Studio - Đồng Bộ Cookie</b>
            <span style="font-size:11px; background:#1e293b; color:#94a3b8; padding:1px 6px; border-radius:6px;">v1.1</span>
          </div>
          <button id="muse-close-btn" style="background:transparent; border:none; color:#64748b; font-size:18px; cursor:pointer; padding:0 4px; line-height:1;">✕</button>
        </div>

        <!-- Trạng thái phiên -->
        <div style="background:#0f172a; border:1px solid #1e293b; border-radius:10px; padding:10px 12px; margin-bottom:14px; display:flex; justify-content:space-between; align-items:center;">
          <span style="font-size:12px; color:#94a3b8;">Phiên đăng nhập hiện tại</span>
          <span id="muse-status-tag" style="font-size:12px; font-weight:600; color:#94a3b8;">Đang kiểm tra…</span>
        </div>

        <!-- Cấu hình nhập liệu -->
        <div style="margin-bottom:12px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">Địa chỉ máy chủ (Base URL)</div>
          <input id="muse-cfg-base" type="text" placeholder="http://127.0.0.1:18610"
                 value="${cfg.base || 'http://127.0.0.1:18610'}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; font-family:monospace; outline:none; transition:border-color 0.2s;">
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">API Key bảo mật</div>
          <input id="muse-cfg-key" type="password" placeholder="m2a_..."
                 value="${cfg.key || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; font-family:monospace; outline:none;">
        </div>

        <div style="margin-bottom:16px;">
          <div style="font-size:11px; color:#94a3b8; margin-bottom:5px; font-weight:500;">Tên tài khoản (Ghi chú tùy chọn)</div>
          <input id="muse-cfg-label" type="text" placeholder="Ví dụ: Tài khoản 1"
                 value="${cfg.label || ''}"
                 style="width:100%; box-sizing:border-box; padding:8px 11px; background:#0b1120; border:1px solid #334155;
                        border-radius:8px; color:#f1f5f9; font-size:12px; outline:none;">
        </div>

        <!-- Các nút thao tác -->
        <div style="display:flex; flex-direction:column; gap:8px;">
          <button id="muse-action-push" style="width:100%; padding:10px 0; border:none; border-radius:10px;
                  background:linear-gradient(135deg, #0284c7, #2563eb); color:#fff;
                  font-weight:600; font-size:13px; cursor:pointer; box-shadow:0 4px 14px rgba(37, 99, 235, 0.4);
                  transition:all 0.2s;">
            ⚡ Đồng Bộ Vào MuseAI Studio
          </button>

          <div style="display:flex; gap:8px;">
            <button id="muse-action-copy" style="flex:1; padding:7px 0; border:1px solid #334155; border-radius:8px;
                    background:#0f172a; color:#cbd5e1; font-size:12px; cursor:pointer; transition:all 0.2s;">
              📋 Sao Chép Cookie
            </button>
            <button id="muse-action-save" style="flex:1; padding:7px 0; border:1px solid #334155; border-radius:8px;
                    background:#0f172a; color:#cbd5e1; font-size:12px; cursor:pointer; transition:all 0.2s;">
              💾 Lưu Cấu Hình
            </button>
          </div>
        </div>

        <!-- Chân bảng điều khiển -->
        <div style="margin-top:14px; padding-top:10px; border-top:1px solid #1e293b; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:#64748b;">
          <span>Cấu hình tự động lưu trên trình duyệt</span>
          <span>Nhấn Esc hoặc dấu ✕ để đóng</span>
        </div>
      </div>
    `;

    document.body.appendChild(panel);
    updateStatusBadge();

    const baseIn = document.getElementById('muse-cfg-base');
    const keyIn = document.getElementById('muse-cfg-key');
    const labelIn = document.getElementById('muse-cfg-label');

    function saveInputs() {
      const newCfg = {
        base: normBase(baseIn.value),
        key: keyIn.value.trim(),
        label: labelIn.value.trim(),
      };
      saveCfg(newCfg);
      return newCfg;
    }

    baseIn.addEventListener('change', saveInputs);
    keyIn.addEventListener('change', saveInputs);
    labelIn.addEventListener('change', saveInputs);

    document.getElementById('muse-close-btn').onclick = () => panel.remove();
    document.getElementById('muse-action-save').onclick = () => {
      saveInputs();
      showToast('✓ Cấu hình đã được lưu!', 'ok');
    };

    document.getElementById('muse-action-copy').onclick = async () => {
      const { cookies } = await getCookies();
      const str = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
      if (str) {
        if (typeof GM_setClipboard === 'function') GM_setClipboard(str);
        showToast('✓ Đã sao chép toàn bộ Cookie vào Clipboard!', 'ok');
      } else {
        showToast('Chưa phát hiện được Cookie nào!', 'err');
      }
    };

    document.getElementById('muse-action-push').onclick = function () {
      saveInputs();
      executePush(this);
    };
  }

  function mountFloatingTrigger() {
    if (document.getElementById('muse2api-floating-ball')) return;
    const ball = document.createElement('div');
    ball.id = 'muse2api-floating-ball';
    ball.innerHTML = `
      <div style="position:fixed; bottom:24px; right:24px; z-index:999990;
                  width:42px; height:42px; border-radius:50%; background:linear-gradient(135deg, #0284c7, #2563eb);
                  color:#fff; display:flex; align-items:center; justify-content:center;
                  cursor:pointer; box-shadow:0 8px 24px rgba(37, 99, 235, 0.45); font-size:18px;
                  user-select:none; transition:transform 0.2s, box-shadow 0.2s;"
           title="Nhấn để mở bảng nhập Cookie MuseAI"
           onmouseover="this.style.transform='scale(1.08)'"
           onmouseout="this.style.transform='scale(1)'">
        ⚡
      </div>
    `;
    ball.addEventListener('click', togglePanel);
    document.body.appendChild(ball);
  }

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('🚀 Mở bảng nhập Cookie MuseAI', togglePanel);
    GM_registerMenuCommand('⚡ Đồng bộ ngay tài khoản hiện tại', () => executePush());
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountFloatingTrigger);
  } else {
    mountFloatingTrigger();
  }
})();
