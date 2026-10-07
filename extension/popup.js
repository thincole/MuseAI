/* MuseAI Cookie Importer —— Trích xuất cookie từ muse.ai và đồng bộ về máy chủ MuseAI Studio.
 *
 * Sử dụng chrome.cookies API của trình duyệt để đọc được toàn bộ
 * 4 cookie xác thực cốt lõi có cờ HttpOnly:
 * (hatch_sess / hatch_gw / hatch_vml / hatch_native_auth_device).
 */

const $ = (id) => document.getElementById(id);
const STORE = 'muse2api_ext_cfg';

const AUTH_COOKIE = 'hatch_sess';

function log(html, cls) {
  const el = $('log');
  el.className = 'show';
  el.innerHTML = cls ? `<span class="${cls}">${html}</span>` : html;
}

/* Chuẩn hóa địa chỉ máy chủ: loại bỏ dấu gạch chéo cuối và /v1 */
function normBase(v) {
  let s = (v || '').trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) {
    s = (/^(127\.|100\.|10\.|192\.168\.|localhost)/i.test(s) ? 'http://' : 'https://') + s;
  }
  s = s.replace(/\/+$/, '');
  s = s.replace(/\/v1$/i, '');
  return s;
}

const DEFAULT_KEY = ''; // Không có key mặc định: dán key trong file .env (MUSE2API_KEY) của server
const DEFAULT_BASE = 'http://127.0.0.1:18610';

async function loadCfg() {
  const o = await chrome.storage.local.get(STORE);
  const c = o[STORE] || {};
  $('base').value = c.base || DEFAULT_BASE;
  $('key').value = c.key || DEFAULT_KEY;
  if (c.label) $('label').value = c.label;
  
  // Tự động nhận diện cấu hình nếu người dùng đang mở trang Studio hoặc Admin
  if (!c.base) {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const u = tab && tab.url ? new URL(tab.url) : null;
      if (u && (/\/admin/.test(u.pathname) || /\/studio/.test(u.pathname) || u.port === '18610')) {
        $('base').value = u.origin;
        const k = new URLSearchParams(u.search).get('key');
        if (k) $('key').value = k;
      }
    } catch (e) {
      $('base').value = DEFAULT_BASE;
    }
  }
}

async function saveCfg() {
  await chrome.storage.local.set({
    [STORE]: {
      base: normBase($('base').value),
      key: $('key').value.trim(),
      label: $('label').value.trim(),
    },
  });
}

async function grabCookies() {
  const all = await chrome.cookies.getAll({ domain: 'muse.ai' });
  const out = {}, exp = {};
  for (const c of all) {
    const dom = (c.domain || '').replace(/^\./, '');
    if (!dom.endsWith('muse.ai')) continue;
    out[c.name] = c.value;
    if (c.expirationDate) exp[c.name] = Math.floor(c.expirationDate);
  }
  return { cookies: out, expires: exp };
}

async function run() {
  const base = normBase($('base').value);
  const key = $('key').value.trim();
  const label = $('label').value.trim();

  if (!base) return log('Vui lòng điền địa chỉ máy chủ (Base URL)', 'bad');
  if (!key) return log('Vui lòng điền API Key bảo mật', 'bad');

  $('go').disabled = true;
  log('Đang trích xuất Cookie từ muse.ai…');

  try {
    const { cookies, expires } = await grabCookies();
    const names = Object.keys(cookies);
    if (!names.length) {
      return log('Không tìm thấy Cookie của muse.ai.\n'
                 + 'Vui lòng mở tab mới và đăng nhập https://muse.ai/ trên trình duyệt này trước khi thực hiện.', 'bad');
    }
    if (!cookies[AUTH_COOKIE]) {
      return log('Chưa tìm thấy Cookie phiên đăng nhập (hatch_sess).\n'
                 + 'Vui lòng mở tab https://muse.ai/ và đăng nhập tài khoản trước khi nhập.', 'bad');
    }

    let warningNotice = '';
    if (!cookies['hatch_vml']) {
      warningNotice = '\n⚠️ Lưu ý: Tài khoản chưa có hatch_vml (tài khoản mới đang ở bước hỏi tên "What\'s your name?"). Hãy bấm nút [Continue] trên tab Muse.ai để kích hoạt đầy đủ.';
    }

    log(`Đã đọc ${names.length} cookie hợp lệ. Đang đồng bộ tới máy chủ ${base} …`);

    const r = await fetch(base + '/admin/accounts', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + key,
      },
      body: JSON.stringify({ label, cookies, expires }),
    });

    const text = await r.text();
    let data;
    try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }

    if (r.status === 401) {
      return log('API Key không hợp lệ (Máy chủ trả về 401).\n'
                 + 'Vui lòng mở ứng dụng MuseAI Studio -> Tab Cài đặt để sao chép API Key chuẩn.', 'bad');
    }
    if (!r.ok) {
      return log(`Đồng bộ thất bại: Mã lỗi HTTP ${r.status}\n${text.slice(0, 300)}`, 'bad');
    }

    const a = (data.added && data.added[0]) || {};
    await saveCfg();
    const expText = a.expires_at ? new Date(a.expires_at * 1000).toLocaleString('vi-VN') : 'Tự động tính toán (~48h)';
    log(`✓ ĐỒNG BỘ THÀNH CÔNG!\n`
        + `Tên tài khoản: ${a.label || label || '(Mặc định)'}\n`
        + `Mã tài khoản (ID): ${a.id || '?'}\n`
        + `Số lượng Cookie: ${a.cookie_count || names.length} cookies\n`
        + `Hạn phiên làm việc: ${expText}\n`
        + warningNotice
        + (data.warning ? `\nLưu ý máy chủ: ${data.warning}` : ''), 'ok');
  } catch (e) {
    log('Đã xảy ra lỗi: ' + (e && e.message ? e.message : String(e))
        + '\n\nCác nguyên nhân phổ biến:\n'
        + '• Địa chỉ máy chủ điền sai hoặc ứng dụng MuseAI chưa được mở\n'
        + '• Máy chủ chưa bật hoặc cổng 18610 bị chặn tường lửa\n'
        + '• Trình duyệt chặn kết nối tới địa chỉ máy chủ nội bộ', 'bad');
  } finally {
    $('go').disabled = false;
  }
}

$('go').addEventListener('click', run);
loadCfg();
