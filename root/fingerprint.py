"""Hồ sơ "dấu vân tay" trình duyệt ngẫu nhiên cho từng lần mở Chrome (công cụ Lấy Cookie).

Mỗi trình duyệt mở lên nhận 1 hồ sơ KHÁC NHAU nhưng HỢP LÝ (các giá trị khớp nhau như một máy Windows thật):
  - màn hình / cửa sổ / tỉ lệ hiển thị (DPR)
  - số nhân CPU, RAM, nhà sản xuất + tên card đồ hoạ (WebGL)
  - nhiễu nhẹ cho Canvas và Audio (mỗi trình duyệt 1 "mã hạt" riêng)
  - múi giờ, ngôn ngữ

KHÔNG làm (và vì sao):
  - Không đổi phiên bản Chrome / User-Agent: phiên bản trong UA phải khớp với Chrome thật đang chạy, nếu UA nói phiên bản
    khác thì còn dễ bị nhận ra hơn.
  - Không đổi "dấu vân tay TLS" (JA4): do chính Chrome quyết định. Chrome đã tự xáo thứ tự phần mở rộng TLS nên JA3 khác
    nhau mỗi lần mở, nhưng JA4 chỉ đổi khi đổi bản Chrome. Muốn khác JA4 phải dùng bản Chrome/trình duyệt khác.
"""
from __future__ import annotations

import json
import random

# (rộng, cao) màn hình phổ biến trên máy Windows
SCREENS = [(1920, 1080), (1920, 1080), (1366, 768), (1536, 864), (1440, 900), (1600, 900), (2560, 1440), (1680, 1050)]
DPRS = [1.0, 1.0, 1.0, 1.25, 1.5]
CORES = [4, 6, 8, 8, 12, 16]
MEMORY = [4, 8, 8]
GPUS = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 (0x00001F82) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB (0x000010DE) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3050 (0x00002507) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 (0x00001F08) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x00009A49) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Series (0x000067DF) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
]
LANGS = [
    ("en-US", ["en-US", "en"]),
    ("en-US", ["en-US", "en", "vi"]),
    ("vi-VN", ["vi-VN", "vi", "en-US", "en"]),
    ("en-GB", ["en-GB", "en-US", "en"]),
]
TIMEZONE = "Asia/Ho_Chi_Minh"     # khớp IP proxy Việt Nam (lệch múi giờ so với IP là dấu hiệu dễ bị phát hiện)


def make_profile(rng: random.Random | None = None) -> dict:
    """Tạo 1 hồ sơ ngẫu nhiên hợp lý. Truyền rng=random.Random(seed) nếu muốn lặp lại được."""
    r = rng or random.SystemRandom()
    sw, sh = r.choice(SCREENS)
    # cửa sổ nhỏ hơn màn hình một chút (thanh tác vụ + viền)
    win_w = min(sw, r.choice([1200, 1280, 1366, 1440, 1536, 1600, 1760]))
    win_h = min(sh - 80, r.choice([760, 800, 860, 900, 960]))
    vendor, renderer = r.choice(GPUS)
    lang, langs = r.choice(LANGS)
    return {
        "screen": [sw, sh],
        "window": [win_w, win_h],
        "dpr": r.choice(DPRS),
        "cores": r.choice(CORES),
        "memory": r.choice(MEMORY),
        "gpu_vendor": vendor,
        "gpu_renderer": renderer,
        "lang": lang,
        "langs": langs,
        "timezone": TIMEZONE,
        "seed": r.randrange(1, 2 ** 31),
    }


def chrome_args(p: dict) -> list[str]:
    """Tham số dòng lệnh Chrome tương ứng (cửa sổ, tỉ lệ hiển thị, ngôn ngữ)."""
    return [
        f"--window-size={p['window'][0]},{p['window'][1]}",
        f"--force-device-scale-factor={p['dpr']}",
        f"--lang={p['lang']}",
    ]


def init_script(p: dict) -> str:
    """JS chạy trước mọi trang: đặt các giá trị hồ sơ + nhiễu Canvas/Audio. Giá trị lấy từ hồ sơ (JSON) nên không ghép chuỗi lỏng lẻo."""
    cfg = json.dumps({k: p[k] for k in ("screen", "cores", "memory", "gpu_vendor", "gpu_renderer", "langs", "seed")})
    return "(function(){var C=" + cfg + r""";
  function def(o,k,v){try{Object.defineProperty(o,k,{get:function(){return v;},configurable:true});}catch(e){}}
  def(Navigator.prototype,'hardwareConcurrency',C.cores);
  def(Navigator.prototype,'deviceMemory',C.memory);
  def(Navigator.prototype,'languages',Object.freeze(C.langs.slice()));
  def(Navigator.prototype,'language',C.langs[0]);
  def(Navigator.prototype,'platform','Win32');
  def(Screen.prototype,'width',C.screen[0]); def(Screen.prototype,'height',C.screen[1]);
  def(Screen.prototype,'availWidth',C.screen[0]); def(Screen.prototype,'availHeight',C.screen[1]-40);
  // WebGL: nhà sản xuất / tên card (UNMASKED_VENDOR_WEBGL=37445, UNMASKED_RENDERER_WEBGL=37446)
  function patchGL(proto){ if(!proto) return; var orig=proto.getParameter;
    proto.getParameter=function(n){ if(n===37445) return C.gpu_vendor; if(n===37446) return C.gpu_renderer; return orig.apply(this,arguments); }; }
  patchGL(window.WebGLRenderingContext&&WebGLRenderingContext.prototype); patchGL(window.WebGL2RenderingContext&&WebGL2RenderingContext.prototype);
  // Canvas: nhiễu rất nhẹ, cố định theo mã hạt của hồ sơ
  var s=C.seed>>>0; function rnd(){ s=(s*1664525+1013904223)>>>0; return s/4294967296; }
  var oData=HTMLCanvasElement.prototype.toDataURL, oImg=CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData=function(){ var d=oImg.apply(this,arguments);
    try{ for(var i=0;i<d.data.length;i+=4*97){ d.data[i]=(d.data[i]+(rnd()<0.5?0:1))&255; } }catch(e){} return d; };
  HTMLCanvasElement.prototype.toDataURL=function(){ try{ var c=this.getContext&&this.getContext('2d'); if(c&&this.width>0&&this.height>0){
      var px=c.getImageData(0,0,1,1); c.putImageData(px,0,0); } }catch(e){} return oData.apply(this,arguments); };
  // Audio: nhiễu nhẹ
  if(window.AudioBuffer){ var oCh=AudioBuffer.prototype.getChannelData; AudioBuffer.prototype.getChannelData=function(){ var a=oCh.apply(this,arguments);
      try{ if(!this.__fp){ for(var i=0;i<a.length;i+=211){ a[i]+= (rnd()-0.5)*1e-7; } this.__fp=1; } }catch(e){} return a; }; }
})();"""


def apply_profile(page, p: dict):
    """Áp hồ sơ lên trình duyệt (DrissionPage ChromiumPage) TRƯỚC khi mở trang nào."""
    page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=init_script(p))
    try:
        page.run_cdp("Emulation.setTimezoneOverride", timezoneId=p["timezone"])
    except Exception:  # noqa: BLE001
        pass
    try:
        page.run_cdp("Emulation.setLocaleOverride", locale=p["lang"])
    except Exception:  # noqa: BLE001
        pass
