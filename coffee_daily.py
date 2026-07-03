# -*- coding: utf-8 -*-
"""
Coffee Daily 2.0 — дневен фундаментален бюлетин за арабика
Тегли: време по региони (Open-Meteo), USD/BRL и KC=F (Yahoo), COT (CFTC).
ICE запасите се обновяват ръчно в константите по-долу (няма безплатен API).
"""

import os
import json
import datetime
import urllib.request
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf

# ============================================================
# РЪЧНИ КОНСТАНТИ — обновявай ~веднъж седмично
# ============================================================
ICE_STOCKS_BAGS = 377_465        # ICE certified arabica stocks (чувала)
ICE_STOCKS_DATE = "01.07.2026"   # към коя дата е числото
ICE_STOCKS_NOTE = "най-ниско от март 2024"

# Бавен фундамент (USDA FAS)
FUND = {
    "crop_year": "2025/26",
    "world_prod": "178.8 млн. чувала (рекорд, +2%)",
    "world_cons": "173.9 млн. (рекорд)",
    "end_stocks": "20.1 млн. (5-та поредна година надолу)",
    "brazil_arabica": "38.0 млн. (-13% суша/жега)",
    "brazil_stocks": "0.485 млн. (критично нисък буфер)",
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
# Региони арабика (Бразилия)
# ============================================================
REGIONS = [
    ("Varginha (Sul de Minas)",      -21.55, -45.43),
    ("Patrocínio (Cerrado Mineiro)", -18.94, -46.99),
    ("Franca (Alta Mogiana, SP)",    -20.54, -47.40),
    ("Manhuaçu (Matas de Minas)",    -20.26, -42.03),
]

FROST_LIMIT = 4.0   # °C — под това вдигаме флаг за слана
DRY_LIMIT = 2.0     # mm за 7 дни — под това е "сухо"


def def http_json(url, timeout=15, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (coffee-daily)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            import time
            time.sleep(2 * (i + 1))
    raise last


def http_text(url, timeout=30):
    req = urllib.request.Request(req_url := url, headers={"User-Agent": "coffee-daily/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


# ------------------------------------------------------------
# 1) Времето — Open-Meteo (безплатно, без ключ)
# ------------------------------------------------------------
def fetch_weather():
    out = []
    for name, lat, lon in REGIONS:
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&daily=temperature_2m_min,temperature_2m_max,precipitation_sum"
                "&forecast_days=7&timezone=America%2FSao_Paulo"
            )
            d = http_json(url)["daily"]
            tmin = min(d["temperature_2m_min"])
            tmax = max(d["temperature_2m_max"])
            rain = sum(d["precipitation_sum"])
            frost_days = sum(1 for t in d["temperature_2m_min"] if t <= FROST_LIMIT)
            out.append({
                "name": name, "tmin": tmin, "tmax": tmax,
                "rain": rain, "frost_days": frost_days,
            })
        except Exception as e:
            out.append({"name": name, "error": str(e)})
    return out


# ------------------------------------------------------------
# 2) Пазар — KC=F и USD/BRL от Yahoo
# ------------------------------------------------------------
def fetch_market():
    kc = yf.Ticker("KC=F").history(period="5d")
    brl = yf.Ticker("BRL=X").history(period="5d")

    kc_last = float(kc["Close"].iloc[-1])
    kc_prev = float(kc["Close"].iloc[-2]) if len(kc) > 1 else kc_last
    kc_chg = (kc_last / kc_prev - 1) * 100

    brl_last = float(brl["Close"].iloc[-1])
    brl_prev = float(brl["Close"].iloc[-2]) if len(brl) > 1 else brl_last
    brl_chg = (brl_last / brl_prev - 1) * 100

    return {
        "kc": kc_last, "kc_chg": kc_chg,
        "brl": brl_last, "brl_chg": brl_chg,
    }


# ------------------------------------------------------------
# 3) COT — CFTC legacy futures-only (обновява се петък)
# ------------------------------------------------------------
def fetch_cot():
    try:
        txt = http_text("https://www.cftc.gov/dea/newcot/deafut.txt")
        for line in txt.splitlines():
            if "COFFEE C" in line.upper():
                parts = [p.strip().strip('"') for p in line.split(",")]
                # legacy формат: [7]=OI [8]=NC long [9]=NC short [11]=Comm long [12]=Comm short
                oi = int(parts[7])
                nc_long = int(parts[8])
                nc_short = int(parts[9])
                c_long = int(parts[11])
                c_short = int(parts[12])
                as_of = parts[2]
                return {
                    "as_of": as_of, "oi": oi,
                    "nc_long": nc_long, "nc_short": nc_short,
                    "nc_net": nc_long - nc_short,
                    "c_long": c_long, "c_short": c_short,
                    "c_net": c_long - c_short,
                }
    except Exception as e:
        return {"error": str(e)}
    return {"error": "COFFEE C не е намерен в CFTC файла"}


# ------------------------------------------------------------
# 4) Модел за справедлива цена → вердикт
# ------------------------------------------------------------
def compute(mkt, weather, today):
    base = 200          # базова котва
    tight = 30          # тесен физически пазар (ниски глобални запаси)
    stocks_adj = 15     # ниски ICE запаси
    balance = -10       # очакван излишък 2026/27 (рекордна реколта)

    # сезонен фактор: прозорец за слана юни–август
    month = today.month
    seasonal = 25 if month in (6, 7, 8) else (10 if month in (5, 9) else 0)

    # ако прогнозата реално показва слана — още отгоре
    frost_flag = any(w.get("frost_days", 0) > 0 for w in weather)
    if frost_flag:
        seasonal += 15

    # валутен фактор: силен реал (нисък USD/BRL) = бразилците задържат = бичи
    fx_adj = round((5.50 - mkt["brl"]) * 20)
    fx_adj = max(min(fx_adj, 20), -20)

    fair = base + tight + seasonal + stocks_adj + balance + fx_adj
    lo, hi = round(fair * 0.90), round(fair * 1.10)

    if mkt["kc"] > hi:
        verdict, color = "НАДЦЕНЕНО (пазарът е над модела)", "#b71c1c"
    elif mkt["kc"] < lo:
        verdict, color = "ПОДЦЕНЕНО (пазарът е под модела)", "#1b5e20"
    else:
        verdict, color = "СПРАВЕДЛИВО ОЦЕНЕНО (в диапазона)", "#5d4037"

    return {
        "fair": fair, "lo": lo, "hi": hi,
        "verdict": verdict, "color": color,
        "components": [
            ("Базова котва", base),
            ("(+) Теснота", tight),
            ("(+) Време/сезон (слана, авто по месец)", seasonal),
            ("(+) Ниски запаси", stocks_adj),
            ("(-) Баланс 2026/27", balance),
            ("(±) Валута (авто от BRL)", fx_adj),
        ],
        "frost_flag": frost_flag,
    }


# ------------------------------------------------------------
# 5) HTML
# ------------------------------------------------------------
def build_html(mkt, weather, cot, m, today):
    def row(k, v, bold=False, bg=""):
        st = "font-weight:bold;" if bold else ""
        bgst = f"background:{bg};" if bg else ""
        return (f'<tr><td style="padding:4px 8px;{bgst}">{k}</td>'
                f'<td style="padding:4px 8px;text-align:right;{st}{bgst}">{v}</td></tr>')

    # времето
    wrows = ""
    for w in weather:
        if "error" in w:
            wrows += f'<tr><td colspan="4" style="padding:4px 8px;color:#b71c1c">{w["name"]}: грешка</td></tr>'
            continue
        frost = ("❄️ %d дни" % w["frost_days"]) if w["frost_days"] else "—"
        dry = " (сухо)" if w["rain"] < DRY_LIMIT else ""
        wrows += (
            f'<tr><td style="padding:4px 8px">{w["name"]}</td>'
            f'<td style="padding:4px 8px;text-align:right">{w["tmin"]:.0f}–{w["tmax"]:.0f}°C</td>'
            f'<td style="padding:4px 8px;text-align:right">{w["rain"]:.0f} mm{dry}</td>'
            f'<td style="padding:4px 8px;text-align:right">{frost}</td></tr>'
        )

    # COT
    if "error" in cot:
        cot_html = f'<p style="color:#b71c1c">COT недостъпен: {cot["error"]}</p>'
    else:
        net_dir = "ЛОНГ" if cot["nc_net"] > 0 else "ШОРТ"
        cot_html = f"""
        <table style="width:100%;border-collapse:collapse;font-size:14px">
        {row("Фондове дълги", f"{cot['nc_long']:,}")}
        {row("Фондове къси", f"{cot['nc_short']:,}")}
        {row("Фондове НЕТО", f"{cot['nc_net']:+,} ({net_dir})", bold=True, bg="#fff8e1")}
        {row("Комерсиални нето", f"{cot['c_net']:+,}")}
        {row("Open Interest", f"{cot['oi']:,}")}
        {row("Данни към", cot["as_of"] + " (CFTC, седмичен)")}
        </table>"""

    comp_rows = "".join(row(k, f"{v:+d}") for k, v in m["components"])
    frost_warn = ('<p style="color:#b71c1c;font-weight:bold">⚠️ ПРОГНОЗА ЗА СЛАНА '
                  'в поне един регион през следващите 7 дни!</p>') if m["frost_flag"] else ""

    kc_arrow = "▲" if mkt["kc_chg"] >= 0 else "▼"
    brl_note = ("реалът отслабва → натиск за продажби от Бразилия (мечи)"
                if mkt["brl_chg"] > 0 else
                "реалът укрепва → бразилците задържат (бичи)")

    html = f"""
    <div style="max-width:640px;font-family:Arial,sans-serif;color:#222">
      <div style="background:#3e2723;color:#fff;padding:10px 16px;font-size:18px;font-weight:bold">
        ☕ Coffee Daily — {today.strftime("%d.%m.%Y")}
      </div>
      <div style="background:{m['color']};color:#fff;padding:12px 16px;text-align:center;font-size:17px;font-weight:bold">
        ВЕРДИКТ: {m['verdict']}
      </div>

      <table style="width:100%;border-collapse:collapse;font-size:14px">
        {row("Справедлива цена (модел)", f"{m['fair']} ¢/lb", bold=True)}
        {row("Диапазон", f"{m['lo']} – {m['hi']} ¢/lb")}
        {row("Текуща пазарна (KC=F)", f"{mkt['kc']:.1f} ¢/lb {kc_arrow} {mkt['kc_chg']:+.1f}%", bold=True, bg="#efebe9")}
        {row("USD/BRL", f"{mkt['brl']:.2f} ({mkt['brl_chg']:+.1f}%)")}
      </table>
      <p style="font-size:13px;color:#555;margin:4px 16px">BRL: {brl_note}</p>

      {frost_warn}

      <h3 style="margin:14px 16px 4px">🌦️ Време — арабика региони (7 дни)</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <tr style="background:#efebe9">
          <th style="padding:4px 8px;text-align:left">Регион</th>
          <th style="padding:4px 8px;text-align:right">Мин–Макс</th>
          <th style="padding:4px 8px;text-align:right">Дъжд</th>
          <th style="padding:4px 8px;text-align:right">Слана</th>
        </tr>
        {wrows}
      </table>

      <h3 style="margin:14px 16px 4px">📦 Запаси</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        {row("ICE certified (арабика)", f"{ICE_STOCKS_BAGS:,} чувала", bold=True)}
        {row("Към дата", f"{ICE_STOCKS_DATE} — {ICE_STOCKS_NOTE}")}
      </table>

      <h3 style="margin:14px 16px 4px">📊 COT — позициониране на фондовете</h3>
      {cot_html}

      <h3 style="margin:14px 16px 4px">⚙️ Компоненти на модела (¢/lb)</h3>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        {comp_rows}
      </table>

      <h3 style="margin:14px 16px 4px">🌍 Фундаментал (бавно-менящ се)</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        {row("Реколтна година", FUND["crop_year"])}
        {row("Световно производство", FUND["world_prod"])}
        {row("Световно потребление", FUND["world_cons"])}
        {row("Краен запас (USDA)", FUND["end_stocks"])}
        {row("Бразилия арабика", FUND["brazil_arabica"])}
        {row("Бразилия запас", FUND["brazil_stocks"])}
        {row("Източник", FUND["source"])}
      </table>

      <p style="font-size:11px;color:#999;margin:14px 16px">
        Автоматичен бюлетин. Не е инвестиционен съвет. COT е седмичен (петък),
        ICE запасите са ръчна константа в скрипта.
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
    weather = fetch_weather()
    mkt = fetch_market()
    cot = fetch_cot()
    m = compute(mkt, weather, today)

    subject = "☕ Кафе %s | %s (модел %d, пазар %.0f)" % (
        today.strftime("%d.%m"), m["verdict"].split(" ")[0], m["fair"], mkt["kc"]
    )
    html = build_html(mkt, weather, cot, m, today)
    print(subject)
    send(subject, html)


if __name__ == "__main__":
    main()
