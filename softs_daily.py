# -*- coding: utf-8 -*-
"""
SOFTS Daily 1.0 — дневен бюлетин: КАФЕ + ЗАХАР + КАКАО
- Цени: KC=F, SB=F, CC=F + макро: петрол CL=F, долар DX-Y.NYB (Yahoo)
- Време 7 дни: Бразилия кафе (4 региона), Бразилия CS захар, Зап. Африка какао
- COT за трите стоки (CFTC, седмичен)
- Вердикт модел за кафето; сигнални бележки за захар и какао
Секрети: SMTP_USER, SMTP_PASS, MAIL_TO (същите като досега).
"""

import os
import json
import time
import datetime
import urllib.request
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf

# ============================================================
# РЪЧНИ КОНСТАНТИ — обновявай ~веднъж седмично
# ============================================================
ICE_STOCKS_BAGS = 377_465
ICE_STOCKS_DATE = "01.07.2026"
ICE_STOCKS_NOTE = "най-ниско от март 2024"

# Какао — ръчни седмични данни (няма безплатен API)
COCOA_ARRIVALS = "обнови ръчно"      # напр. "1.42 млн. т сезонно (+8% г/г)"
COCOA_ARRIVALS_DATE = "—"

FUND_COFFEE = {
    "crop_year": "2025/26",
    "world_prod": "178.8 млн. чувала (рекорд, +2%)",
    "world_cons": "173.9 млн. (рекорд)",
    "end_stocks": "20.1 млн. (5-та поредна година надолу)",
    "brazil_arabica": "38.0 млн. (-13% суша/жега)",
    "source": "USDA FAS, 18.12.2025",
}

# ============================================================
# SMTP от GitHub Secrets
# ============================================================
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO = os.environ.get("MAIL_TO", SMTP_USER)

# ============================================================
# Региони по стоки
# ============================================================
COFFEE_REGIONS = [
    ("Varginha (Sul de Minas)",      -21.55, -45.43),
    ("Patrocínio (Cerrado Mineiro)", -18.94, -46.99),
    ("Franca (Alta Mogiana, SP)",    -20.54, -47.40),
    ("Manhuaçu (Matas de Minas)",    -20.26, -42.03),
]
SUGAR_REGIONS = [
    ("Ribeirão Preto (CS, SP)",      -21.18, -47.81),
    ("Uberaba (Triângulo, MG)",      -19.75, -47.93),
]
COCOA_REGIONS = [
    ("San-Pédro (Кот д'Ивоар)",       4.75,  -6.64),
    ("Daloa (Кот д'Ивоар)",           6.88,  -6.45),
    ("Kumasi (Гана)",                 6.69,  -1.62),
]

FROST_LIMIT = 4.0
DRY_LIMIT = 2.0
COCOA_DRY_LIMIT = 10.0   # Зап. Африка: под 10 mm/7д е тревожно сухо за какао

UA = {"User-Agent": "Mozilla/5.0 (compatible; softs-daily/1.0)"}


def http_json(url, timeout=15, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def http_text(url, timeout=20, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


# ------------------------------------------------------------
# 1) Времето — Open-Meteo, 7 дни напред
# ------------------------------------------------------------
def fetch_weather(regions):
    out = []
    for name, lat, lon in regions:
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&daily=temperature_2m_min,temperature_2m_max,precipitation_sum"
                "&forecast_days=7&timezone=auto"
            )
            d = http_json(url)["daily"]
            tmins = [t for t in d["temperature_2m_min"] if t is not None]
            tmaxs = [t for t in d["temperature_2m_max"] if t is not None]
            rains = [p for p in d["precipitation_sum"] if p is not None]
            out.append({
                "name": name,
                "tmin": min(tmins),
                "tmax": max(tmaxs),
                "rain": sum(rains),
                "frost_days": sum(1 for t in tmins if t <= FROST_LIMIT),
            })
        except Exception as e:
            print("WEATHER ERROR:", name, "->", repr(e))
            out.append({"name": name, "error": str(e)})
    return out


# ------------------------------------------------------------
# 2) Пазар — Yahoo
# ------------------------------------------------------------
def yq(ticker):
    h = yf.Ticker(ticker).history(period="5d")
    last = float(h["Close"].iloc[-1])
    prev = float(h["Close"].iloc[-2]) if len(h) > 1 else last
    return {"last": last, "chg": (last / prev - 1) * 100}


def fetch_market():
    m = {}
    for key, tk in [("kc", "KC=F"), ("sb", "SB=F"), ("cc", "CC=F"),
                    ("brl", "BRL=X"), ("oil", "CL=F"), ("dxy", "DX-Y.NYB")]:
        try:
            m[key] = yq(tk)
        except Exception as e:
            print("MARKET ERROR:", tk, "->", repr(e))
            m[key] = {"last": 0.0, "chg": 0.0, "error": str(e)}
    return m


# ------------------------------------------------------------
# 3) COT — CFTC legacy futures-only (петъчен)
# ------------------------------------------------------------
COT_NAMES = {
    "Кафе (KC)":  "COFFEE C",
    "Захар (SB)": "SUGAR NO. 11",
    "Какао (CC)": "COCOA",
}


def fetch_cot_all():
    res = {}
    try:
        txt = http_text("https://www.cftc.gov/dea/newcot/deafut.txt")
        lines = txt.splitlines()
        for label, key in COT_NAMES.items():
            found = None
            for line in lines:
                if key in line.upper():
                    parts = [p.strip().strip('"') for p in line.split(",")]
                    try:
                        found = {
                            "as_of": parts[2],
                            "oi": int(parts[7]),
                            "nc_long": int(parts[8]),
                            "nc_short": int(parts[9]),
                            "nc_net": int(parts[8]) - int(parts[9]),
                        }
                    except Exception as e:
                        found = {"error": f"parse: {e}"}
                    break
            res[label] = found or {"error": "не е намерен"}
    except Exception as e:
        print("COT ERROR:", repr(e))
        for label in COT_NAMES:
            res[label] = {"error": str(e)}
    return res


# ------------------------------------------------------------
# 4) Вердикт модел — само за кафето
# ------------------------------------------------------------
def compute_coffee(mkt, weather, today):
    base, tight, stocks_adj, balance = 200, 30, 15, -10
    month = today.month
    seasonal = 25 if month in (6, 7, 8) else (10 if month in (5, 9) else 0)
    frost_flag = any(w.get("frost_days", 0) > 0 for w in weather)
    if frost_flag:
        seasonal += 15
    fx_adj = round((5.50 - mkt["brl"]["last"]) * 20)
    fx_adj = max(min(fx_adj, 20), -20)

    fair = base + tight + seasonal + stocks_adj + balance + fx_adj
    lo, hi = round(fair * 0.90), round(fair * 1.10)
    kc = mkt["kc"]["last"]
    if kc > hi:
        verdict, color = "НАДЦЕНЕНО", "#b71c1c"
    elif kc < lo:
        verdict, color = "ПОДЦЕНЕНО", "#1b5e20"
    else:
        verdict, color = "СПРАВЕДЛИВО", "#5d4037"
    return {"fair": fair, "lo": lo, "hi": hi, "verdict": verdict,
            "color": color, "frost_flag": frost_flag}


def sugar_notes(mkt, weather):
    notes = []
    dry = [w["name"] for w in weather if "error" not in w and w["rain"] < DRY_LIMIT]
    wet = [w["name"] for w in weather if "error" not in w and w["rain"] > 40]
    if dry:
        notes.append("сухо в CS → жътвата/смилането вървят с пълна пара (мечи къс. срок)")
    if wet:
        notes.append("дъжд в CS → прекъсване на смилането (бичи къс. срок)")
    if "error" not in mkt["oil"] and mkt["oil"]["chg"] > 1.5:
        notes.append("петролът силно нагоре → етанол паритетът тегли тръстика от захарта (бичи)")
    elif "error" not in mkt["oil"] and mkt["oil"]["chg"] < -1.5:
        notes.append("петролът надолу → повече тръстика към захар (мечи)")
    if not notes:
        notes.append("без силен сигнал днес; следи UNICA (двуседмичен) и Индия (мусон/износ)")
    return notes


def cocoa_notes(weather):
    notes = []
    dry = [w["name"] for w in weather if "error" not in w and w["rain"] < COCOA_DRY_LIMIT]
    if dry:
        notes.append("тревожно сухо в: " + ", ".join(dry) + " → риск за main crop (бичи)")
    else:
        notes.append("валежите в Зап. Африка изглеждат адекватни (неутрално/мечи)")
    notes.append("следи port arrivals (понеделник) и тримесечния grind — ръчни данни")
    return notes


# ------------------------------------------------------------
# 5) HTML
# ------------------------------------------------------------
def build_html(mkt, w_coffee, w_sugar, w_cocoa, cot, m, today):
    def row(k, v, bold=False, bg=""):
        st = "font-weight:bold;" if bold else ""
        bgst = f"background:{bg};" if bg else ""
        return (f'<tr><td style="padding:4px 8px;{bgst}">{k}</td>'
                f'<td style="padding:4px 8px;text-align:right;{st}{bgst}">{v}</td></tr>')

    def arrow(x):
        return "▲" if x >= 0 else "▼"

    def price_row(label, q, unit):
        if "error" in q:
            return row(label, "н.д.")
        return row(label, f"{q['last']:.2f} {unit} {arrow(q['chg'])} {q['chg']:+.1f}%")

    def weather_table(ws, frost_col=True):
        head_frost = '<th style="padding:4px 8px;text-align:right">Слана</th>' if frost_col else ""
        rows = ""
        for w in ws:
            if "error" in w:
                rows += (f'<tr><td colspan="4" style="padding:4px 8px;color:#b71c1c">'
                         f'{w["name"]}: {w["error"]}</td></tr>')
                continue
            frost = (("❄️ %d дни" % w["frost_days"]) if w["frost_days"] else "—")
            fcell = f'<td style="padding:4px 8px;text-align:right">{frost}</td>' if frost_col else ""
            rows += (
                f'<tr><td style="padding:4px 8px">{w["name"]}</td>'
                f'<td style="padding:4px 8px;text-align:right">{w["tmin"]:.0f}–{w["tmax"]:.0f}°C</td>'
                f'<td style="padding:4px 8px;text-align:right">{w["rain"]:.0f} mm</td>'
                f'{fcell}</tr>'
            )
        return f"""
        <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#efebe9">
          <th style="padding:4px 8px;text-align:left">Регион</th>
          <th style="padding:4px 8px;text-align:right">Мин–Макс</th>
          <th style="padding:4px 8px;text-align:right">Дъжд 7д</th>
          {head_frost}
        </tr>{rows}</table>"""

    def cot_table():
        rows = ""
        as_of = ""
        for label, c in cot.items():
            if "error" in c:
                rows += row(label, "н.д.")
                continue
            d = "ЛОНГ" if c["nc_net"] > 0 else "ШОРТ"
            rows += row(label, f"{c['nc_net']:+,} нето ({d}) | L {c['nc_long']:,} / S {c['nc_short']:,}")
            as_of = c["as_of"]
        foot = row("Данни към", as_of + " (CFTC, седмичен)") if as_of else ""
        return f'<table style="width:100%;border-collapse:collapse;font-size:13px">{rows}{foot}</table>'

    def notes_list(items):
        lis = "".join(f'<li style="margin:2px 0">{x}</li>' for x in items)
        return f'<ul style="font-size:13px;margin:4px 16px;padding-left:20px">{lis}</ul>'

    frost_warn = ('<p style="color:#b71c1c;font-weight:bold;margin:8px 16px">⚠️ ПРОГНОЗА ЗА СЛАНА '
                  'в кафе регион през следващите 7 дни!</p>') if m["frost_flag"] else ""

    brl_note = ("реалът отслабва → натиск за продажби от Бразилия (мечи за KC/SB)"
                if mkt["brl"]["chg"] > 0 else
                "реалът укрепва → бразилците задържат (бичи за KC/SB)")
    dxy_note = ("силен долар → общ натиск върху softs" if mkt["dxy"]["chg"] > 0
                else "доларът отслабва → попътен вятър за softs")

    html = f"""
    <div style="max-width:640px;font-family:Arial,sans-serif;color:#222">
      <div style="background:#3e2723;color:#fff;padding:10px 16px;font-size:18px;font-weight:bold">
        🌱 SOFTS Daily — {today.strftime("%d.%m.%Y")}
      </div>
      <div style="background:{m['color']};color:#fff;padding:10px 16px;text-align:center;font-size:16px;font-weight:bold">
        КАФЕ ВЕРДИКТ: {m['verdict']} (модел {m['fair']}, диапазон {m['lo']}–{m['hi']})
      </div>

      <h3 style="margin:12px 16px 4px">💰 Цени</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        {price_row("Кафе арабика (KC=F)", mkt["kc"], "¢/lb")}
        {price_row("Захар №11 (SB=F)", mkt["sb"], "¢/lb")}
        {price_row("Какао (CC=F)", mkt["cc"], "$/t")}
        {price_row("Петрол WTI (CL=F)", mkt["oil"], "$/bbl")}
        {price_row("Долар индекс (DXY)", mkt["dxy"], "")}
        {price_row("USD/BRL", mkt["brl"], "")}
      </table>
      <p style="font-size:12px;color:#555;margin:4px 16px">BRL: {brl_note}<br>DXY: {dxy_note}</p>

      {frost_warn}

      <h3 style="margin:14px 16px 4px">☕ КАФЕ — време, Бразилия (7 дни)</h3>
      {weather_table(w_coffee, frost_col=True)}
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        {row("ICE certified запаси", f"{ICE_STOCKS_BAGS:,} чувала ({ICE_STOCKS_DATE}, {ICE_STOCKS_NOTE})")}
      </table>

      <h3 style="margin:14px 16px 4px">🍬 ЗАХАР — време, Center-South (7 дни)</h3>
      {weather_table(w_sugar, frost_col=True)}
      {notes_list(sugar_notes(mkt, w_sugar))}

      <h3 style="margin:14px 16px 4px">🍫 КАКАО — време, Западна Африка (7 дни)</h3>
      {weather_table(w_cocoa, frost_col=False)}
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        {row("Port arrivals (ръчно)", f"{COCOA_ARRIVALS} ({COCOA_ARRIVALS_DATE})")}
      </table>
      {notes_list(cocoa_notes(w_cocoa))}

      <h3 style="margin:14px 16px 4px">📊 COT — нето позиция на фондовете</h3>
      {cot_table()}

      <h3 style="margin:14px 16px 4px">🌍 Кафе фундаментал (бавен)</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        {row("Реколтна година", FUND_COFFEE["crop_year"])}
        {row("Св. производство", FUND_COFFEE["world_prod"])}
        {row("Св. потребление", FUND_COFFEE["world_cons"])}
        {row("Краен запас (USDA)", FUND_COFFEE["end_stocks"])}
        {row("Бразилия арабика", FUND_COFFEE["brazil_arabica"])}
        {row("Източник", FUND_COFFEE["source"])}
      </table>

      <p style="font-size:11px;color:#999;margin:14px 16px">
        Автоматичен бюлетин. Не е инвестиционен съвет. COT е седмичен (петък).
        ICE запаси и какао arrivals са ръчни константи в скрипта.
      </p>
    </div>
    """
    return html


# ------------------------------------------------------------
# 6) Изпращане
# ------------------------------------------------------------
def send(subject, html):
    if not (SMTP_USER and SMTP_PASS):
        print("ГРЕШКА: липсват SMTP_USER / SMTP_PASS secrets!")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, [a.strip() for a in MAIL_TO.split(",")], msg.as_string())
    print("Изпратено до:", MAIL_TO)
    return True


def main():
    today = datetime.date.today()
    w_coffee = fetch_weather(COFFEE_REGIONS)
    w_sugar = fetch_weather(SUGAR_REGIONS)
    w_cocoa = fetch_weather(COCOA_REGIONS)
    mkt = fetch_market()
    cot = fetch_cot_all()
    m = compute_coffee(mkt, w_coffee, today)

    subject = "🌱 SOFTS %s | KC %s %.0f | SB %.2f | CC %.0f" % (
        today.strftime("%d.%m"), m["verdict"], mkt["kc"]["last"],
        mkt["sb"]["last"], mkt["cc"]["last"]
    )
    html = build_html(mkt, w_coffee, w_sugar, w_cocoa, cot, m, today)
    print(subject)
    send(subject, html)


if __name__ == "__main__":
    main()
