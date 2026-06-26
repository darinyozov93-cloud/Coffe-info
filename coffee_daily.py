# -*- coding: utf-8 -*-
"""
КАФЕ - дневен фундаментален дайджест по имейл
=============================================
Какво прави всеки ден:
  1) Тегли живата цена на арабика (ICE 'KC', Yahoo: KC=F) и курса USD/BRL.
  2) Преизчислява "справедливата цена" по същия модел като Excel файла.
  3) Авто-настройва два компонента: сезонна премия за слана (по месец) и
     валутна корекция (спрямо живия реал). Останалите са твои допускания.
  4) Праща ти имейл с вердикт: ПОДЦЕНЕНО / СПРАВЕДЛИВО / НАДЦЕНЕНО.

Слабо-менящите се фундаментали (производство, запаси, баланс) обновявай
РЪЧНО в секция FUNDAMENTALS долу, когато излезе нов USDA/CONAB доклад.

Преди първо пускане:
  pip install yfinance
  и задай променливите на средата (виж "НАСТРОЙКА НА ИМЕЙЛ" по-долу).
"""

import os
import sys
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# =====================================================================
# 1) МОДЕЛ ЗА СПРАВЕДЛИВА ЦЕНА  (същата логика като Excel, в cent/lb)
#    Тези РЕДАКТИРАШ ти. Котвата е най-важното допускане.
# =====================================================================
ANCHOR              = 200   # Базова котва (mid-cycle "нормална" цена)
PREMIUM_TIGHTNESS   = 30    # Премия теснота (stock-to-use на дъна)
PREMIUM_STOCKS      = 15    # Премия ниски сертифицирани запаси
ADJ_BALANCE         = -10   # Корекция за баланса на годината (излишък = минус)
BAND                = 0.10  # +/- диапазон около точката (10%)

# Валутна корекция - авто спрямо живия реал:
NEUTRAL_BRL         = 5.40  # "неутрален" курс
BRL_SENS            = 33.0  # cent/lb на 1.00 движение в BRL (слаб реал = минус)

# Сезонна премия за време (слана) - авто по месец (бразилска зима юни-юли):
def weather_premium(month):
    if month in (6, 7):      # пик на риска от слана
        return 25
    if month in (5, 8, 9):   # рамо (край реколта / цъфтеж)
        return 12
    return 5                 # извън сезон

# Резервни стойности, ако тегленето падне (за да тръгне имейлът все пак):
FALLBACK_ARABICA = 280.0    # cent/lb
FALLBACK_BRL     = 5.40

# =====================================================================
# 2) БАВНО-МЕНЯЩИ СЕ ФУНДАМЕНТАЛИ (обнови ръчно при нов USDA доклад)
# =====================================================================
FUNDAMENTALS = {
    "Реколтна година":          "2025/26",
    "Световно производство":    "178.8 млн. чувала (рекорд, +2%)",
    "Световно потребление":     "173.9 млн. (рекорд)",
    "Краен запас (USDA)":       "20.1 млн. (5-та поредна година надолу)",
    "Бразилия арабика":         "38.0 млн. (-13% суша/жега)",
    "Бразилия запас":           "0.485 млн. (критично нисък буфер)",
    "Източник":                 "USDA FAS, 18.12.2025",
}

# =====================================================================
# 3) НАСТРОЙКА НА ИМЕЙЛ - чете се от променливи на средата (НЕ пиши пароли в кода!)
#    Пример за Gmail (трябва App Password, не нормалната парола):
#      export SMTP_HOST=smtp.gmail.com
#      export SMTP_PORT=465
#      export SMTP_USER=tvoя_адрес@gmail.com
#      export SMTP_PASS=шестнадесетцифрен_app_password
#      export MAIL_TO=tvoя_адрес@gmail.com
# =====================================================================
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_TO   = os.environ.get("MAIL_TO", SMTP_USER)


# ---------------------------------------------------------------------
def fetch_market():
    """Връща (arabica_cent_lb, usdbrl, stale_flag). Резервни стойности при грешка."""
    try:
        import yfinance as yf
        arabica = float(yf.Ticker("KC=F").fast_info["last_price"])
        brl     = float(yf.Ticker("USDBRL=X").fast_info["last_price"])
        return arabica, brl, False
    except Exception as e:
        print("ВНИМАНИЕ: тегленето падна (%s). Ползвам резервни стойности." % e)
        return FALLBACK_ARABICA, FALLBACK_BRL, True


def compute(arabica, brl, today):
    wx = weather_premium(today.month)
    fx = round((NEUTRAL_BRL - brl) * BRL_SENS, 1)   # слаб реал (висок BRL) -> минус
    fair = ANCHOR + PREMIUM_TIGHTNESS + wx + PREMIUM_STOCKS + ADJ_BALANCE + fx
    low, high = fair * (1 - BAND), fair * (1 + BAND)
    if arabica < low:
        verdict, tag = "ПОДЦЕНЕНО", "пазарът е под модела"
    elif arabica > high:
        verdict, tag = "НАДЦЕНЕНО", "пазарът е над модела"
    else:
        verdict, tag = "СПРАВЕДЛИВО", "в справедливия диапазон"
    return {
        "wx": wx, "fx": fx, "fair": fair, "low": low, "high": high,
        "verdict": verdict, "tag": tag,
    }


def build_html(arabica, brl, m, today, stale):
    rows = "".join(
        "<tr><td style='padding:3px 10px'>%s</td><td style='padding:3px 10px'>%s</td></tr>" % (k, v)
        for k, v in FUNDAMENTALS.items()
    )
    comp = [
        ("Базова котва", ANCHOR),
        ("(+) Теснота", PREMIUM_TIGHTNESS),
        ("(+) Време (слана, авто по месец)", m["wx"]),
        ("(+) Ниски запаси", PREMIUM_STOCKS),
        ("(-) Баланс", ADJ_BALANCE),
        ("(-) Валута (авто от BRL)", m["fx"]),
    ]
    comp_rows = "".join(
        "<tr><td style='padding:2px 10px'>%s</td><td style='padding:2px 10px;text-align:right'>%+d</td></tr>" % (n, v)
        for n, v in comp
    )
    color = {"ПОДЦЕНЕНО": "#2E7D32", "НАДЦЕНЕНО": "#C62828", "СПРАВЕДЛИВО": "#6F4E37"}[m["verdict"]]
    stale_note = "<p style='color:#C62828'>⚠️ Цените са резервни (тегленето падна или пазарът е затворен).</p>" if stale else ""
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;color:#222">
      <h2 style="background:#3B2417;color:#fff;padding:10px;margin:0">☕ Кафе — дневен фундаментал · {today:%d.%m.%Y}</h2>
      {stale_note}
      <div style="background:{color};color:#fff;padding:14px;font-size:18px;font-weight:bold;text-align:center">
        ВЕРДИКТ: {m['verdict']} <span style="font-weight:normal;font-size:13px">({m['tag']})</span>
      </div>
      <table style="border-collapse:collapse;margin-top:12px;width:100%">
        <tr><td style="padding:4px 10px">Справедлива цена (модел)</td><td style="padding:4px 10px;text-align:right"><b>{m['fair']:.0f} ¢/lb</b></td></tr>
        <tr><td style="padding:4px 10px">Диапазон</td><td style="padding:4px 10px;text-align:right">{m['low']:.0f} – {m['high']:.0f} ¢/lb</td></tr>
        <tr style="background:#F3E7D8"><td style="padding:4px 10px"><b>Текуща пазарна (KC=F)</b></td><td style="padding:4px 10px;text-align:right"><b>{arabica:.1f} ¢/lb</b></td></tr>
        <tr><td style="padding:4px 10px">USD/BRL</td><td style="padding:4px 10px;text-align:right">{brl:.2f}</td></tr>
      </table>
      <h3 style="margin:16px 0 4px">Компоненти на модела (¢/lb)</h3>
      <table style="border-collapse:collapse;width:100%;font-size:13px">{comp_rows}</table>
      <h3 style="margin:16px 0 4px">Фундаментал (бавно-менящ се)</h3>
      <table style="border-collapse:collapse;width:100%;font-size:13px">{rows}</table>
      <p style="font-size:11px;color:#777;margin-top:16px">
        Опростен модел, не пазарна истина — стойността зависи изцяло от Базовата котва, която задаваш ти.
        НЕ е инвестиционен съвет. Фючърсите носят висок риск от загуба.
      </p>
    </div>
    """


def send(subject, html):
    if not (SMTP_USER and SMTP_PASS):
        print("ГРЕШКА: липсват SMTP_USER / SMTP_PASS в средата. Имейлът НЕ е изпратен.")
        print("(Скриптът смята коректно — само пощата не е конфигурирана.)")
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
    arabica, brl, stale = fetch_market()
    m = compute(arabica, brl, today)
    html = build_html(arabica, brl, m, today, stale)
    subject = "☕ Кафе %s | %s (модел %.0f vs пазар %.0f ¢/lb)" % (
        today.strftime("%d.%m"), m["verdict"], m["fair"], arabica)
    print(subject)
    send(subject, html)


if __name__ == "__main__":
    main()
