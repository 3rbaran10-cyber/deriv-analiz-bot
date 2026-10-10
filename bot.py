import os
import io, json, base64, time, math, requests, threading, re, sqlite3, ssl, warnings
from PIL import Image
from http.server import BaseHTTPRequestHandler, HTTPServer

import websocket
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

warnings.filterwarnings("ignore")

# ==========================================
# AYARLAR
# ==========================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

GEMINI_MODEL = "gemini-3.1-pro-preview"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

GEMINI_LOCK = threading.Lock()
SON_ISTEK_ZAMANI = [0.0]
MIN_ISTEK_ARASI = 3.0

ADMIN_ID = 5504006147

# ==========================================
# DERIV
# ==========================================
DERIV_ENDPOINTS = [
    "wss://api.derivws.com/trading/v1/options/ws/public",
]

SYMBOLS = [
    ("Crash 600 Index",  "CRASH600"),
    ("Crash 900 Index",  "CRASH900"),
    ("Crash 1000 Index", "CRASH1000"),
    ("Boom 600 Index",   "BOOM600"),
    ("Boom 900 Index",   "BOOM900"),
    ("Boom 1000 Index",  "BOOM1000"),
]
SYMBOL_MAP = {kod: isim for isim, kod in SYMBOLS}

TF_M30, TF_M15, TF_M5, TF_M1 = 1800, 900, 300, 60
GRAFIK_MUM_SAYISI = 100
ATR_PERIOD = 50
VARSAYILAN_ATR = 50

ATR_MIN_CARPAN = 1.0
ATR_MAX_CARPAN = 6.0
MIN_RR_TP1 = 1.2
MIN_GUVEN = 70

_ATR_CACHE, _ATR_CACHE_TTL = {}, 60
_CANDLE_CACHE, _CANDLE_CACHE_TTL = {}, 30

# ==========================================
# HEALTH SERVER
# ==========================================
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()

# ==========================================
# DERIV ISTEK
# ==========================================
_deriv_lock = threading.Lock()

def _tek_deneme(endpoint, payload, timeout=30):
    ws = None
    try:
        ws = websocket.create_connection(
            endpoint, timeout=timeout,
            sslopt={"cert_reqs": ssl.CERT_NONE},
            enable_multithread=True,
            skip_utf8_validation=True,
        )
        ws.send(json.dumps(payload))
        start = time.time()
        while time.time() - start < timeout:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw: break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("req_id") == payload.get("req_id"):
                if "error" in msg:
                    print(f"Deriv error: {msg['error']}", flush=True)
                    return None
                return msg
        return None
    except Exception as e:
        print(f"Deriv hatasi: {str(e)[:150]}", flush=True)
        return None
    finally:
        if ws:
            try: ws.close()
            except: pass

def deriv_call(payload, timeout=30):
    with _deriv_lock:
        for endpoint in DERIV_ENDPOINTS:
            r = _tek_deneme(endpoint, payload, timeout)
            if r is not None:
                return r
        print("❌ Deriv endpoint çalışmadı.", flush=True)
        return None

def get_candles(symbol, granularity, count=100):
    key = (symbol, granularity)
    now = time.time()
    cached = _CANDLE_CACHE.get(key)
    if cached and (now - cached[1]) < _CANDLE_CACHE_TTL:
        return cached[0]
    resp = deriv_call({
        "ticks_history": symbol, "adjust_start_time": 1,
        "count": count, "end": "latest",
        "granularity": granularity, "style": "candles", "req_id": 1
    })
    if not resp: return None
    candles = resp.get("candles") or []
    if candles: _CANDLE_CACHE[key] = (candles, now)
    return candles

def get_current_price(symbol):
    resp = deriv_call({
        "ticks_history": symbol, "style": "ticks",
        "count": 1, "end": "latest", "req_id": 2
    })
    if not resp: return None
    prices = (resp.get("history") or {}).get("prices") or []
    return float(prices[-1]) if prices else None

def calculate_atr(candles, period=50):
    if not candles or len(candles) < 2: return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"]); l = float(candles[i]["low"])
        pc = float(candles[i-1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs: return None
    n = min(period, len(trs))
    son = sorted(trs[-n:])
    mid = len(son) // 2
    medyan = (son[mid] + son[mid-1]) / 2 if len(son) % 2 == 0 else son[mid]
    return round(medyan, 4)

# ==========================================
# TREND BELIRLEME
# ==========================================
def trend_yonu(candles, son_n=30):
    if not candles or len(candles) < son_n:
        return "YATAY"
    son = candles[-son_n:]
    yari = len(son) // 2
    ilk_ort = sum(float(c['close']) for c in son[:yari]) / yari
    son_ort = sum(float(c['close']) for c in son[yari:]) / (len(son) - yari)
    fark = son_ort - ilk_ort
    esik = ilk_ort * 0.0005
    if fark > esik:
        return "YUKARI"
    elif fark < -esik:
        return "ASAGI"
    return "YATAY"

# ==========================================
# BLEED FILTRESI (9/10)
# ==========================================
def bleed_kontrol(m1_candles, yon):
    if not m1_candles or len(m1_candles) < 10:
        return False, 0
    son10 = m1_candles[-10:]
    kapanislar = [float(c['close']) for c in son10]
    yukari = sum(1 for i in range(1, len(kapanislar)) if kapanislar[i] > kapanislar[i-1])
    asagi  = sum(1 for i in range(1, len(kapanislar)) if kapanislar[i] < kapanislar[i-1])
    if yon == "SHORT" and yukari >= 9:
        return True, yukari
    if yon == "LONG" and asagi >= 9:
        return True, asagi
    return False, 0

# ==========================================
# LIKIDITE AVI DEDEKTORU
# ==========================================
def likidite_avi_kontrol(m1_candles, yon):
    if not m1_candles or len(m1_candles) < 3:
        return False, ""
    son_mum = m1_candles[-1]
    try:
        o = float(son_mum['open']); cl = float(son_mum['close'])
        h = float(son_mum['high']); l = float(son_mum['low'])
    except:
        return False, ""
    boy = h - l
    if boy <= 0:
        return False, ""
    if yon == "SHORT":
        ust_wick = h - max(o, cl)
        if ust_wick > boy * 0.6 and cl < o:
            return True, "Sahte yukari kirilim (uzun ust wick)"
    if yon == "LONG":
        alt_wick = min(o, cl) - l
        if alt_wick > boy * 0.6 and cl > o:
            return True, "Sahte asagi kirilim (uzun alt wick)"
    return False, ""

# ==========================================
# TREND UYUM FILTRESI
# ==========================================
def trend_uyum_kontrol(m30_candles, m15_candles, yon):
    t_m30 = trend_yonu(m30_candles, 30)
    t_m15 = trend_yonu(m15_candles, 30)
    if yon == "SHORT":
        if t_m30 == "YUKARI" and t_m15 == "YUKARI":
            return False, f"M30/M15 YUKARI, SHORT celisiyor"
        if t_m30 == "ASAGI" or t_m15 == "ASAGI":
            return True, f"M30={t_m30}, M15={t_m15}"
    if yon == "LONG":
        if t_m30 == "ASAGI" and t_m15 == "ASAGI":
            return False, f"M30/M15 ASAGI, LONG celisiyor"
        if t_m30 == "YUKARI" or t_m15 == "YUKARI":
            return True, f"M30={t_m30}, M15={t_m15}"
    return True, f"M30={t_m30}, M15={t_m15}"

# ==========================================
# SPIKE BILGISI
# ==========================================
def son_spike_bilgisi(m1_candles, yon):
    try:
        if not m1_candles or len(m1_candles) < 20:
            return "bilinmiyor"
        son = m1_candles[-100:] if len(m1_candles) >= 100 else m1_candles
        degisimler = []
        for c in son:
            o = float(c['open']); cl = float(c['close'])
            degisimler.append(cl - o)
        if yon == "SHORT":
            idx = degisimler.index(min(degisimler))
            mesafe = len(son) - 1 - idx
            buyukluk = abs(min(degisimler))
            return f"Son buyuk dusus {mesafe} mum once ({round(buyukluk, 2)} puan)."
        else:
            idx = degisimler.index(max(degisimler))
            mesafe = len(son) - 1 - idx
            buyukluk = max(degisimler)
            return f"Son buyuk yukselis {mesafe} mum once ({round(buyukluk, 2)} puan)."
    except:
        return "bilinmiyor"

# ==========================================
# GRAFIK
# ==========================================
_plot_lock = threading.Lock()

def _ciz_candles(ax, candles):
    if not candles:
        ax.text(0.5, 0.5, "veri yok", ha='center', va='center',
                transform=ax.transAxes, fontsize=12)
        return
    for i, c in enumerate(candles):
        try:
            o = float(c['open']); h = float(c['high'])
            l = float(c['low']);  cl = float(c['close'])
        except: continue
        renk = '#26a69a' if cl >= o else '#ef5350'
        ax.plot([i, i], [l, h], color=renk, linewidth=0.7)
        alt = min(o, cl)
        yuk = abs(cl - o) if cl != o else (h - l) * 0.01
        ax.add_patch(Rectangle((i - 0.3, alt), 0.6, yuk,
                                facecolor=renk, edgecolor=renk))
    ax.grid(True, alpha=0.3, linestyle='--')
    ax.set_xlim(-1, len(candles))
    ax.tick_params(labelsize=8)

def draw_chart_4tf(symbol, isim, fiyat, atr_m30, atr_m15, atr_m5, atr_m1):
    m30 = get_candles(symbol, TF_M30, GRAFIK_MUM_SAYISI)
    m15 = get_candles(symbol, TF_M15, GRAFIK_MUM_SAYISI)
    m5  = get_candles(symbol, TF_M5,  GRAFIK_MUM_SAYISI)
    m1  = get_candles(symbol, TF_M1,  GRAFIK_MUM_SAYISI)

    if not m1:
        return None

    with _plot_lock:
        fig, axes = plt.subplots(1, 4, figsize=(26, 7))
        fig.patch.set_facecolor('white')

        fig.suptitle(
            f"{isim}   |   FIYAT: {fiyat}   |   M5 ATR (SL/TP): {atr_m5}",
            fontsize=17, fontweight='bold', y=0.985
        )

        tf_bilgileri = [
            (axes[0], m30, "1) M30", "ANA TREND",    atr_m30, "#ff7f0e"),
            (axes[1], m15, "2) M15", "ORTA VADE",    atr_m15, "#2ca02c"),
            (axes[2], m5,  "3) M5",  "KISA VADE",    atr_m5,  "#9467bd"),
            (axes[3], m1,  "4) M1",  "GIRIS ZAMANI", atr_m1,  "#d62728"),
        ]

        for ax, candles, numara, rol, atr, renk in tf_bilgileri:
            _ciz_candles(ax, candles)
            baslik = f"{numara}  {rol}\nATR: {atr}"
            ax.set_title(baslik, fontsize=13, fontweight='bold',
                         color=renk, pad=12)
            for spine in ax.spines.values():
                spine.set_edgecolor(renk)
                spine.set_linewidth(3)
            ax.set_facecolor('#fafafa')

        fig.text(0.5, 0.015,
                 "1 = M30 (Ana Trend)      |      2 = M15 (Orta Vade)      |      "
                 "3 = M5 (Kisa Vade)      |      4 = M1 (Giris Zamani)",
                 ha='center', fontsize=12, style='italic',
                 color='#222222', fontweight='bold')

        plt.tight_layout(rect=[0, 0.05, 1, 0.94])
        buf = io.BytesIO()
        plt.savefig(buf, format="PNG", dpi=90, bbox_inches='tight',
                    facecolor='white')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

# ==========================================
# KIRPMA (SL/TP)
# ==========================================
def kirp_sl_tp(symbol, giris, gemini_sl, gemini_tp1, gemini_tp2, atr_m5):
    try:
        giris_f = float(str(giris).replace(",", "."))
        sl_f    = float(str(gemini_sl).replace(",", "."))
        tp1_f   = float(str(gemini_tp1).replace(",", "."))
        tp2_f   = float(str(gemini_tp2).replace(",", "."))
    except: return None

    atr = atr_m5 if atr_m5 and atr_m5 > 0 else 10
    min_m = atr * ATR_MIN_CARPAN
    max_m = atr * ATR_MAX_CARPAN

    sl_mesafe  = abs(giris_f - sl_f)
    tp1_mesafe = abs(giris_f - tp1_f)
    tp2_mesafe = abs(giris_f - tp2_f)

    if sl_mesafe < min_m:
        sl_k = min_m
    elif sl_mesafe > max_m:
        sl_k = max_m
    else:
        sl_k = sl_mesafe

    tp1_min = sl_k * MIN_RR_TP1
    tp1_k = tp1_mesafe if tp1_mesafe >= tp1_min else tp1_min

    tp2_min = tp1_k * 1.2
    tp2_k = tp2_mesafe if tp2_mesafe >= tp2_min else tp2_min

    return {"giris": round(giris_f, 4), "atr": atr,
            "sl_mesafe": round(sl_k, 4),
            "tp1_mesafe": round(tp1_k, 4),
            "tp2_mesafe": round(tp2_k, 4)}

# ==========================================
# PROMPT
# ==========================================
PROMPT_TEMPLATE = """Sen dunyanin en iyi {sembol} analiz uzmanisin. 15+ yillik deneyimli profesyonelsin. Smart Money konseptlerini (ICT) derinlemesine bilirsin.

=== GORSEL ACIKLAMASI ===
Sana {sembol} icin TEK bir gorsel gonderiliyor.
Bu gorsel 4 PARCAYA BOLUNMUS, soldan saga:

  [1] M30 - TURUNCU cerceveli - ANA TREND
  [2] M15 - YESIL cerceveli   - ORTA VADE
  [3] M5  - MOR cerceveli     - KISA VADE
  [4] M1  - KIRMIZI cerceveli - GIRIS ZAMANI

Her grafigin USTUNDE numara, rol ve ATR yaziyor.

=== ANALIZ SIRASI ===
1. M30 -> Ana trend nedir?
2. M15 -> Orta vade trendi M30'u onayliyor mu?
3. M5  -> Kisa vade yapi
4. M1  -> KESIN giris zamani

=== CANLI VERILER ===
- GUNCEL FIYAT: {fiyat}
- M30 ATR: {atr_m30} puan
- M15 ATR: {atr_m15} puan
- M5  ATR: {atr_m5} puan (SL/TP guvenlik sinirlari)
- M1  ATR: {atr_m1} puan
- SON SPIKE: {spike_bilgi}

=== SL/TP NASIL BELIRLEMELISIN ===
1. Grafiklerdeki destek/direnc/FVG/likidite seviyelerine bak
2. SL'yi YAPISAL seviyeye koy (destek alti / direnc ustu)
3. SL cok dar olmasin: en az 1 x M5 ATR ({min_sl} puan)
4. SL cok genis olmasin: en fazla 6 x M5 ATR ({max_sl} puan)
5. TP1 en az SL'nin 1.2 kati (R/R >= 1:1.2)

=== CRASH/BOOM DAVRANISI ===
- Crash: ani DUSUS spike'lari (asagi) -> SHORT bias
- Boom: ani YUKSELIS spike'lari (yukari) -> LONG bias
- BLEED: Crash'te yavas yukari, Boom'da yavas asagi
- Bleed evresinde pozisyon acma
- Spike zamanlamasi RASTGELE

=== KARAR KURALLARI ===
1. M30, M15 celisiyorsa -> BEKLE
2. Guven %70 altinda -> BEKLE
3. M1'de bleed varsa -> BEKLE
4. SL/TP icin YAPISAL seviyeleri kullan

=== CIKTI FORMATI ===
SADECE gecerli JSON. Sayilarda NOKTA. Turkce. String giris/stop_loss.

=== JSON SEMASI ===
- sembol, yon ("LONG"|"SHORT"|"BEKLE"), guven (0-100)
- giris, stop_loss (string)
- take_profit (array, 2 eleman)
- trend_m30, trend_m15, trend_m5, trend_m1
- vwap_durumu, fvg_tespit, likidite_durumu
- destekler, direncler, formasyonlar, kullanilan_teknikler (array)
- kisa_analiz, gerekce
- kalan_mum (1-3), mum_yonu, hareket_aciklamasi, sonraki_hamle, uyari
"""

# ==========================================
# SQLITE
# ==========================================
DB_PATH = "/tmp/telegram_offset.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER)")
        conn.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cid INTEGER, sembol TEXT, yon TEXT, guven INTEGER,
            giris TEXT, stop_loss TEXT, tp1 TEXT, tp2 TEXT,
            kalan_mum INTEGER, mum_yonu TEXT, kisa_analiz TEXT,
            ts INTEGER, sonuc TEXT DEFAULT NULL)""")
        conn.commit(); conn.close()
        print("DB hazir", flush=True)
    except Exception as e:
        print(f"DB hatasi: {e}", flush=True)

def get_offset():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT value FROM state WHERE key='offset'")
        row = cur.fetchone(); conn.close()
        return row[0] if row else 0
    except: return 0

def save_offset(o):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES ('offset', ?)", (o,))
        conn.commit(); conn.close()
    except: pass

def save_analysis(cid, sembol, a):
    try:
        tps = a.get("take_profit") or []
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""INSERT INTO analyses
            (cid, sembol, yon, guven, giris, stop_loss, tp1, tp2,
             kalan_mum, mum_yonu, kisa_analiz, ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, sembol, a.get("yon"), a.get("guven"),
             a.get("giris"), a.get("stop_loss"),
             tps[0] if len(tps) > 0 else None,
             tps[1] if len(tps) > 1 else None,
             a.get("kalan_mum"), a.get("mum_yonu"),
             a.get("kisa_analiz"), int(time.time())))
        conn.commit()
        rid = cur.lastrowid; conn.close()
        return rid
    except Exception as e:
        print(f"save hatasi: {e}", flush=True); return None

def update_sonuc(aid, sonuc):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("UPDATE analyses SET sonuc=? WHERE id=? AND sonuc IS NULL", (sonuc, aid))
        d = cur.rowcount; conn.commit(); conn.close()
        return d > 0
    except: return False

def get_gecmis(cid, limit=10):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("SELECT id, sembol, yon, guven, sonuc, ts FROM analyses WHERE cid=? ORDER BY id DESC LIMIT ?", (cid, limit))
        rows = cur.fetchall(); conn.close()
        return rows
    except: return []

def get_istatistik(cid):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""SELECT COUNT(*),
            SUM(CASE WHEN sonuc='tuttu' THEN 1 ELSE 0 END),
            SUM(CASE WHEN sonuc='tutmadi' THEN 1 ELSE 0 END)
            FROM analyses WHERE cid=? AND yon != 'BEKLE'""", (cid,))
        total, tuttu, tutmadi = cur.fetchone()
        total = total or 0; tuttu = tuttu or 0; tutmadi = tutmadi or 0
        cur = conn.execute("""SELECT sembol, COUNT(*),
            SUM(CASE WHEN sonuc='tuttu' THEN 1 ELSE 0 END),
            SUM(CASE WHEN sonuc='tutmadi' THEN 1 ELSE 0 END)
            FROM analyses WHERE cid=? AND yon != 'BEKLE'
            GROUP BY sembol ORDER BY COUNT(*) DESC""", (cid,))
        semboller = cur.fetchall(); conn.close()
        return {"total": total, "tuttu": tuttu, "tutmadi": tutmadi, "semboller": semboller}
    except: return {"total": 0, "tuttu": 0, "tutmadi": 0, "semboller": []}

# ==========================================
# TELEGRAM
# ==========================================
def send_msg(cid, text, parse_mode=None, reply_markup=None):
    try:
        p = {"chat_id": cid, "text": text}
        if parse_mode: p["parse_mode"] = parse_mode
        if reply_markup: p["reply_markup"] = reply_markup
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=p, timeout=10)
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_msg: {e}", flush=True); return None

def send_photo(cid, pb, caption="", reply_markup=None):
    try:
        d = {"chat_id": cid, "caption": caption[:1024]}
        if reply_markup: d["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                          data=d, files={"photo": ("chart.png", pb)}, timeout=60)
        return r.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"send_photo: {e}", flush=True); return None

def edit_reply_markup(cid, mid, rm=None):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
                      json={"chat_id": cid, "message_id": mid, "reply_markup": rm or {"inline_keyboard": []}},
                      timeout=10)
    except: pass

def edit_message_text(cid, mid, text, pm=None, rm=None):
    try:
        p = {"chat_id": cid, "message_id": mid, "text": text}
        if pm: p["parse_mode"] = pm
        if rm: p["reply_markup"] = rm
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText", json=p, timeout=10)
    except: pass

def temizle_sayi(d):
    if d is None: return d
    s = str(d).strip().replace(" ", "")
    if s.count(",") > 1: s = s.replace(",", "")
    if s.count(".") > 1: s = s.replace(".", "", s.count(".") - 1)
    if "," in s and "." not in s: s = s.replace(",", ".")
    return s

# ==========================================
# RATE LIMIT
# ==========================================
USER_COOLDOWN = {}
RATE_LIMIT_SECONDS = 15

def check_rate_limit(cid):
    now = time.time()
    last = USER_COOLDOWN.get(cid, 0)
    if now - last < RATE_LIMIT_SECONDS:
        kalan = int(RATE_LIMIT_SECONDS - (now - last))
        send_msg(cid, f"⏳ Çok hızlı. {kalan} saniye bekleyin.")
        return False
    USER_COOLDOWN[cid] = now
    return True

# ==========================================
# GEMINI
# ==========================================
def gemini_istek_at(url, payload, headers):
    global SON_ISTEK_ZAMANI
    with GEMINI_LOCK:
        gecen = time.time() - SON_ISTEK_ZAMANI[0]
        if gecen < MIN_ISTEK_ARASI:
            time.sleep(MIN_ISTEK_ARASI - gecen)
        resp = requests.post(url, headers=headers, json=payload, timeout=300)
        SON_ISTEK_ZAMANI[0] = time.time()
        return resp

def analyze_chart(image_bytes, cid, symbol, isim, fiyat,
                  atr_m30, atr_m15, atr_m5, atr_m1, spike_bilgi):
    print(f"Analiz: {isim} (V10)", flush=True)
    min_sl = round(atr_m5 * ATR_MIN_CARPAN, 2)
    max_sl = round(atr_m5 * ATR_MAX_CARPAN, 2)
    prompt_text = PROMPT_TEMPLATE.format(
        sembol=isim, fiyat=fiyat,
        atr_m30=atr_m30, atr_m15=atr_m15, atr_m5=atr_m5, atr_m1=atr_m1,
        spike_bilgi=spike_bilgi,
        min_sl=min_sl, max_sl=max_sl)

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((1920, 1920))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()
    del img, buf

    payload = {
        "contents": [{"parts": [
            {"text": prompt_text},
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
        ]}],
        "generationConfig": {
            "temperature": 1.0,
            "maxOutputTokens": 16384,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "sembol": {"type": "string"},
                    "yon": {"type": "string", "enum": ["LONG", "SHORT", "BEKLE"]},
                    "guven": {"type": "integer"},
                    "giris": {"type": "string"},
                    "stop_loss": {"type": "string"},
                    "take_profit": {"type": "array", "items": {"type": "string"}},
                    "trend_m30": {"type": "string"},
                    "trend_m15": {"type": "string"},
                    "trend_m5": {"type": "string"},
                    "trend_m1": {"type": "string"},
                    "vwap_durumu": {"type": "string"},
                    "fvg_tespit": {"type": "string"},
                    "likidite_durumu": {"type": "string"},
                    "destekler": {"type": "array", "items": {"type": "string"}},
                    "direncler": {"type": "array", "items": {"type": "string"}},
                    "formasyonlar": {"type": "array", "items": {"type": "string"}},
                    "kullanilan_teknikler": {"type": "array", "items": {"type": "string"}},
                    "kisa_analiz": {"type": "string"},
                    "gerekce": {"type": "string"},
                    "kalan_mum": {"type": "integer"},
                    "mum_yonu": {"type": "string"},
                    "hareket_aciklamasi": {"type": "string"},
                    "sonraki_hamle": {"type": "string"},
                    "uyari": {"type": "string"}
                },
                "required": ["sembol","yon","guven","giris","stop_loss",
                             "take_profit","kisa_analiz","gerekce",
                             "kalan_mum","mum_yonu","hareket_aciklamasi"]
            }
        }
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    bekleme = [5, 15, 30]
    for deneme in range(len(bekleme) + 1):
        try:
            resp = gemini_istek_at(GEMINI_URL, payload, headers)
            print(f"Gemini HTTP {resp.status_code} ({deneme+1})", flush=True)
            if resp.status_code == 200:
                return json_parse_et(resp, cid)
            elif resp.status_code in (429, 503):
                if deneme < len(bekleme):
                    send_msg(cid, f"⏳ Model yoğun. {bekleme[deneme]} sn...")
                    time.sleep(bekleme[deneme]); continue
                send_msg(cid, "❌ Model yanıt vermiyor."); return None
            else:
                send_msg(cid, f"❌ Gemini ({resp.status_code}): {resp.text[:300]}")
                return None
        except Exception as e:
            send_msg(cid, f"❌ Analiz Hatası: {str(e)[:200]}"); return None
    return None

def json_parse_et(resp, cid):
    r = resp.json()
    try:
        text = r["candidates"][0]["content"]["parts"][0]["text"].strip()
    except:
        fr = "?"
        try: fr = r["candidates"][0].get("finishReason", "?")
        except: pass
        send_msg(cid, f"❌ Gemini boş cevap. ({fr})"); return None

    if "```json" in text: text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        p = text.split("```")
        if len(p) >= 2:
            text = p[1]
            if text.startswith("json"): text = text[4:]
    text = text.strip()

    try:
        a = json.loads(text)
    except:
        b = text.find('{'); s = text.rfind('}')
        if b != -1 and s > b:
            try: a = json.loads(text[b:s+1])
            except: send_msg(cid, "❌ Bozuk JSON."); return None
        else:
            send_msg(cid, "❌ Gemini JSON vermedi."); return None

    for k in ["giris", "stop_loss"]:
        if k in a: a[k] = temizle_sayi(a[k])
    if isinstance(a.get("take_profit"), list):
        a["take_profit"] = [temizle_sayi(x) for x in a["take_profit"]]

    try: g = int(a.get("guven", 0))
    except: g = 0
    if g < MIN_GUVEN: a["yon"] = "BEKLE"

    try: a["kalan_mum"] = max(1, min(3, int(a.get("kalan_mum", 1))))
    except: a["kalan_mum"] = 1

    return a

# ==========================================
# KART
# ==========================================
def build_card(a, isim):
    e = {"LONG": "🟢", "SHORT": "🔴", "BEKLE": "🟡"}.get(a.get("yon"), "⚪")
    t = ["╔══════════════════════════╗",
         f"║  📊 {isim} ANALİZİ",
         "╠══════════════════════════╣",
         f"║  {e} YÖN: {a.get('yon','?')}",
         f"║  🎯 GÜVEN: %{a.get('guven','?')}",
         "╠══════════════════════════╣"]
    if a.get("yon") != "BEKLE":
        t.append(f"║  💰 GİRİŞ: {a.get('giris','?')}")
        t.append(f"║  🛑 SL:    {a.get('stop_loss','?')}")
        for i, tp in enumerate(a.get("take_profit", []), 1):
            t.append(f"║  ✅ TP{i}:   {tp}")
        if a.get("risk_odul"): t.append(f"║  ⚖️ R/R:   {a['risk_odul']}")
    else:
        t.append("║  ⏸️  Sinyal yok")
        if a.get("uyari"): t.append(f"║  ⚠️  {str(a['uyari'])[:38]}")
    t += ["╚══════════════════════════╝", "", "📈 TREND ANALİZİ"]
    for tf, key in [("M30", "trend_m30"), ("M15", "trend_m15"),
                    ("M5", "trend_m5"), ("M1", "trend_m1")]:
        if a.get(key): t.append(f"• {tf}: {a[key]}")
    if a.get("vwap_durumu"): t.append(f"• VWAP: {a['vwap_durumu']}")
    t += ["", "🎯 SEVİYELER",
          f"🟢 Destek: {', '.join(map(str, a.get('destekler',[]))) or 'Belirsiz'}",
          f"🔴 Direnç: {', '.join(map(str, a.get('direncler',[]))) or 'Belirsiz'}"]
    fvg = a.get("fvg_tespit", ""); lik = a.get("likidite_durumu", "")
    sm = []
    if fvg and fvg.lower() not in ["yok", "belirsiz", ""]: sm.append(f"📦 FVG: {fvg}")
    if lik and lik.lower() not in ["yok", "belirsiz", ""]: sm.append(f"💧 Likidite: {lik}")
    if sm: t += ["", "🧠 SMART MONEY"] + sm
    if a.get("formasyonlar"):
        t += ["", f"🧩 Formasyon: {', '.join(map(str, a['formasyonlar']))}"]
    if a.get("hareket_aciklamasi"):
        t += ["", "⏱️ MUM TAHMİNİ", f"• {a['hareket_aciklamasi']}"]
        if a.get("sonraki_hamle"): t.append(f"• Sonrası: {a['sonraki_hamle']}")
    t += ["", "📝 ÖZET", a.get('kisa_analiz', ''), "", "🔍 GEREKÇELER"]
    for g in str(a.get("gerekce", "")).replace("\\n", "\n").split("\n"):
        if g.strip(): t.append(f"• {g.strip()}")
    t += ["", f"⚠️ {a.get('uyari','Yatırım tavsiyesi değildir.')}"]
    return "\n".join(t)

# ==========================================
# MENU
# ==========================================
def _ana_menu_buton():
    return {"inline_keyboard": [[{"text": "🔙 Ana Menü", "callback_data": "menu:ana"}]]}

def _menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "🔴 Crash 600", "callback_data": "analiz:CRASH600"},
         {"text": "🔴 Crash 900", "callback_data": "analiz:CRASH900"},
         {"text": "🔴 Crash 1000", "callback_data": "analiz:CRASH1000"}],
        [{"text": "🟢 Boom 600", "callback_data": "analiz:BOOM600"},
         {"text": "🟢 Boom 900", "callback_data": "analiz:BOOM900"},
         {"text": "🟢 Boom 1000", "callback_data": "analiz:BOOM1000"}],
        [{"text": "📜 Geçmiş", "callback_data": "menu:gecmis"},
         {"text": "📊 İstatistik", "callback_data": "menu:istatistik"}],
        [{"text": "❓ Yardım", "callback_data": "menu:yardim"}]
    ]}

def _menu_text():
    return ("🤖 *DERIV ANALIZ BOTU V10*\n\n"
            "🎯 Analiz için sembol seç:\n"
            "• M30 + M15 + M5 + M1 grafikleri\n"
            "• M5 ATR ile SL/TP\n"
            "• Bleed + Likidite + Trend filtreleri\n"
            "• Gemini 3.1 Pro analizi\n\n"
            "⬇️ Sembol seç:")

def show_menu(cid, mid=None):
    if mid: edit_message_text(cid, mid, _menu_text(), "Markdown", _menu_keyboard())
    else: send_msg(cid, _menu_text(), "Markdown", _menu_keyboard())

def show_gecmis(cid, mid=None):
    rows = get_gecmis(cid, 10)
    if not rows: text = "📭 *Geçmiş boş*"
    else:
        e = {"LONG": "🟢", "SHORT": "🔴", "BEKLE": "🟡"}
        s = {"tuttu": "✅", "tutmadi": "❌", None: "⏳"}
        sat = ["📜 *SON 10 ANALİZ*", ""]
        for i, (aid, sb, yn, gv, sc, ts) in enumerate(rows, 1):
            tr = time.strftime("%d.%m %H:%M", time.localtime(ts))
            sat.append(f"{i}. {e.get(yn,'⚪')} *{sb}* — {yn} — %{gv}")
            sat.append(f"     {s.get(sc,'?')} {tr}")
        text = "\n".join(sat)
    if mid: edit_message_text(cid, mid, text, "Markdown", _ana_menu_buton())
    else: send_msg(cid, text, "Markdown", _ana_menu_buton())

def show_istatistik(cid, mid=None):
    st = get_istatistik(cid)
    if st["total"] == 0: text = "📭 *İstatistik yok*"
    else:
        sc = st["tuttu"] + st["tutmadi"]
        oran = round(st["tuttu"] / sc * 100, 1) if sc > 0 else 0
        sat = ["📊 *İSTATİSTİK*", "",
               f"📈 Toplam: *{st['total']}*",
               f"✅ Tuttu: *{st['tuttu']}*",
               f"❌ Tutmadı: *{st['tutmadi']}*",
               f"🎯 Başarı: *%{oran}*"]
        if st["semboller"]:
            sat += ["", "📋 *Sembol Bazında:*"]
            for sb, tot, tut, tm in st["semboller"]:
                if tut + tm > 0:
                    r = round(tut / (tut + tm) * 100)
                    sat.append(f"• {sb}: {tut}/{tut+tm} (%{r})")
                else: sat.append(f"• {sb}: {tot} sinyal")
        text = "\n".join(sat)
    if mid: edit_message_text(cid, mid, text, "Markdown", _ana_menu_buton())
    else: send_msg(cid, text, "Markdown", _ana_menu_buton())

def show_yardim(cid, mid=None):
    text = ("❓ *YARDIM - V10*\n\n"
            "📊 *4 Zaman Dilimi:*\n"
            "• 1) M30 → ANA TREND\n"
            "• 2) M15 → ORTA VADE\n"
            "• 3) M5  → KISA VADE\n"
            "• 4) M1  → GIRIS ZAMANI\n\n"
            "🛡️ *Filtreler:*\n"
            "• Bleed filtresi (9/10)\n"
            "• Likidite avı dedektörü\n"
            "• Trend uyum (M30+M15)\n"
            "• Güven eşiği: %70\n\n"
            "📏 *SL/TP:* M5 ATR × 1-6\n"
            "🎯 *TP1:* min SL × 1.2\n\n"
            "⚠️ Yatırım tavsiyesi değildir.")
    if mid: edit_message_text(cid, mid, text, "Markdown", _ana_menu_buton())
    else: send_msg(cid, text, "Markdown", _ana_menu_buton())

# ==========================================
# KOMUTLAR
# ==========================================
def handle_command(cid, text):
    text = (text or "").strip()
    cmd = text.split()[0].lower() if text else ""

    if cmd in ("/menu", "/start"): show_menu(cid); return
    if cmd == "/yardim": show_yardim(cid); return
    if cmd == "/gecmis": show_gecmis(cid); return
    if cmd == "/istatistik": show_istatistik(cid); return
    if cmd == "/analiz":
        p = text.split()
        if len(p) < 2:
            send_msg(cid, "Kullanım: `/analiz CRASH1000`", "Markdown"); return
        kod = p[1].upper().replace(" ", "")
        if kod not in SYMBOL_MAP:
            send_msg(cid, f"❌ Bilinmeyen: `{p[1]}`", "Markdown"); return
        threading.Thread(target=process_analysis, args=(cid, kod), daemon=True).start()
        return
    send_msg(cid, "ℹ️ /menu yazarak başlayın.")

def handle_callback(cq):
    try:
        cid = cq["message"]["chat"]["id"]
        mid = cq["message"]["message_id"]
        data = cq.get("data", "")
        cb_id = cq["id"]
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                      json={"callback_query_id": cb_id}, timeout=10)

        if int(cid) != int(ADMIN_ID):
            send_msg(cid, "🔒 Bu bot özel kullanımdadır."); return

        if data == "menu:ana": show_menu(cid, mid); return
        if data == "menu:gecmis": show_gecmis(cid, mid); return
        if data == "menu:istatistik": show_istatistik(cid, mid); return
        if data == "menu:yardim": show_yardim(cid, mid); return

        if data.startswith("analiz:"):
            kod = data.split(":", 1)[1]
            if kod not in SYMBOL_MAP:
                send_msg(cid, f"❌ Bilinmeyen: {kod}"); return
            if not check_rate_limit(cid): return
            edit_reply_markup(cid, mid, _ana_menu_buton())
            threading.Thread(target=process_analysis, args=(cid, kod), daemon=True).start()
            return

        if data.startswith("sonuc:"):
            _, sonuc, aid_str = data.split(":")
            if update_sonuc(int(aid_str), sonuc):
                edit_reply_markup(cid, mid, None)
                e = "✅" if sonuc == "tuttu" else "❌"
                send_msg(cid, f"{e} Analiz #{aid_str} kaydedildi.")
            else:
                send_msg(cid, "⚠️ Bu analiz zaten işaretlenmiş.")
    except Exception as e:
        print(f"callback: {e}", flush=True)

# ==========================================
# ANALIZ AKISI
# ==========================================
def process_analysis(cid, symbol):
    isim = SYMBOL_MAP[symbol]
    send_msg(cid, f"⏳ *{isim}* analiz ediliyor...\nM30 + M15 + M5 + M1 çekiliyor...", "Markdown")

    fiyat = get_current_price(symbol)
    if not fiyat:
        send_msg(cid, f"❌ {isim} için fiyat alınamadı.")
        return

    m30 = get_candles(symbol, TF_M30, GRAFIK_MUM_SAYISI)
    m15 = get_candles(symbol, TF_M15, GRAFIK_MUM_SAYISI)
    m5  = get_candles(symbol, TF_M5,  GRAFIK_MUM_SAYISI)
    m1  = get_candles(symbol, TF_M1,  GRAFIK_MUM_SAYISI)

    if not m1:
        send_msg(cid, f"❌ {isim} için mum verisi alınamadı.")
        return

    atr_m30 = calculate_atr(m30, ATR_PERIOD) or VARSAYILAN_ATR
    atr_m15 = calculate_atr(m15, ATR_PERIOD) or VARSAYILAN_ATR
    atr_m5  = calculate_atr(m5,  ATR_PERIOD) or VARSAYILAN_ATR
    atr_m1  = calculate_atr(m1,  ATR_PERIOD) or VARSAYILAN_ATR

    spike_bilgi = son_spike_bilgisi(m1, "SHORT" if "CRASH" in symbol else "LONG")
    print(f"{isim} | fiyat={fiyat} | ATR M5={atr_m5}", flush=True)

    png = draw_chart_4tf(symbol, isim, fiyat, atr_m30, atr_m15, atr_m5, atr_m1)
    if not png:
        send_msg(cid, f"❌ Grafik çizilemedi."); return

    a = analyze_chart(png, cid, symbol, isim, fiyat,
                      atr_m30, atr_m15, atr_m5, atr_m1, spike_bilgi)
    if not a:
        send_msg(cid, "❌ Analiz başarısız."); return

    # FILTRE 1: BLEED
    if a.get("yon") in ("LONG", "SHORT"):
        bleed_mi, sayi = bleed_kontrol(m1, a["yon"])
        if bleed_mi:
            print(f"FILTRE BLEED: {isim} {a['yon']} -> BEKLE ({sayi}/9)", flush=True)
            a["yon"] = "BEKLE"
            a["uyari"] = f"M1 bleed evresinde ({sayi}/9 ters hareket)."

    # FILTRE 2: LIKIDITE AVI
    if a.get("yon") in ("LONG", "SHORT"):
        avi_mi, sebep = likidite_avi_kontrol(m1, a["yon"])
        if avi_mi:
            print(f"FILTRE LIKIDITE: {isim} {a['yon']} -> BEKLE ({sebep})", flush=True)
            a["yon"] = "BEKLE"
            a["uyari"] = f"Likidite avi: {sebep}"

    # FILTRE 3: TREND UYUM
    if a.get("yon") in ("LONG", "SHORT"):
        uyum, sebep = trend_uyum_kontrol(m30, m15, a["yon"])
        if not uyum:
            print(f"FILTRE TREND: {isim} {a['yon']} -> BEKLE ({sebep})", flush=True)
            a["yon"] = "BEKLE"
            a["uyari"] = f"Trend celiski: {sebep}"

    if a.get("yon") in ("LONG", "SHORT"):
        a["giris"] = str(fiyat)
        gemini_sl = a.get("stop_loss")
        tps = a.get("take_profit") or []
        tp1 = tps[0] if len(tps) > 0 else None
        tp2 = tps[1] if len(tps) > 1 else None

        if gemini_sl and tp1:
            if not tp2:
                try:
                    g = float(str(fiyat))
                    t1 = float(str(tp1).replace(",", "."))
                    tp2 = str(t1 + abs(g - t1) * 0.3) if a["yon"] == "LONG" else str(t1 - abs(g - t1) * 0.3)
                except: tp2 = tp1

            h = kirp_sl_tp(symbol, fiyat, gemini_sl, tp1, tp2, atr_m5)
            if h:
                if a["yon"] == "SHORT":
                    sl = fiyat + h["sl_mesafe"]
                    t1 = fiyat - h["tp1_mesafe"]
                    t2 = fiyat - h["tp2_mesafe"]
                else:
                    sl = fiyat - h["sl_mesafe"]
                    t1 = fiyat + h["tp1_mesafe"]
                    t2 = fiyat + h["tp2_mesafe"]
                a["giris"] = str(round(fiyat, 4))
                a["stop_loss"] = str(round(sl, 4))
                a["take_profit"] = [str(round(t1, 4)), str(round(t2, 4))]
                rr = h["tp1_mesafe"] / h["sl_mesafe"] if h["sl_mesafe"] > 0 else 0
                a["risk_odul"] = f"1:{round(rr, 2)}"
            else:
                a["yon"] = "BEKLE"; a["uyari"] = "SL/TP hesaplanamadı."
        else:
            a["yon"] = "BEKLE"; a["uyari"] = "SL/TP verisi eksik."

    aid = save_analysis(cid, isim, a)
    kart = build_card(a, isim)

    rm = None
    if a.get("yon") in ("LONG", "SHORT") and aid:
        rm = {"inline_keyboard": [[
            {"text": "✅ Tuttu", "callback_data": f"sonuc:tuttu:{aid}"},
            {"text": "❌ Tutmadı", "callback_data": f"sonuc:tutmadi:{aid}"}
        ]]}

    if len(kart) > 1024:
        send_photo(cid, png, caption=f"{isim} | {a.get('yon')} | %{a.get('guven')}")
        send_msg(cid, kart, reply_markup=rm)
    else:
        send_photo(cid, png, caption=kart, reply_markup=rm)

# ==========================================
# ANA DONGU
# ==========================================
def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    init_db()
    print(f"=== DERIV BOT V10 BASLADI (ID: {ADMIN_ID}) ===", flush=True)
    offset = get_offset()

    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                             params={"offset": offset, "timeout": 30}, timeout=40).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                save_offset(offset)
                if "callback_query" in u:
                    handle_callback(u["callback_query"]); continue
                msg = u.get("message", {})
                cid = msg.get("chat", {}).get("id")
                if not cid: continue
                if int(cid) != int(ADMIN_ID):
                    try: send_msg(cid, "🔒 Bu bot özel kullanımdadır.")
                    except: pass
                    continue
                text = msg.get("text", "")
                if text: handle_command(cid, text); continue
                send_msg(cid, "ℹ️ /menu yazarak başlayın.")
        except Exception as e:
            print(f"=== LOOP: {e} ===", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()
