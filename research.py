#!/usr/bin/env python3
"""
Research Dashboard — taeglicher Datensammler.

Laeuft im GitHub-Actions-Workflow (cron 07:00). Liest data/companies.json,
holt pro Titel Kurse / News / Earnings-Termine von Financial Modeling Prep (FMP),
berechnet technische Indikatoren, fasst optional via Anthropic-API zusammen und
schreibt das Ergebnis nach docs/data/snapshot.json (von GitHub Pages ausgeliefert).

Benoetigte Umgebungsvariablen (als GitHub-Secrets hinterlegen):
  FMP_API_KEY        – Pflicht. Kostenloser Key von financialmodelingprep.com
  ANTHROPIC_API_KEY  – Optional. Nur fuer KI-Zusammenfassungen der News.

Alles ist fehlertolerant: Faellt eine Quelle fuer einen Titel aus, bleibt der Rest
erhalten und das Skript laeuft weiter.
"""

import os
import sys
import json
import time
import math
import datetime as dt
from urllib import request, parse, error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPANIES_FILE = os.path.join(ROOT, "docs", "data", "companies.json")
OUT_FILE = os.path.join(ROOT, "docs", "data", "snapshot.json")

FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FMP_BASE = "https://financialmodelingprep.com/api/v3"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

TODAY = dt.date.today()
HORIZON_DAYS = 95           # Kurshistorie (Tage)
NEWS_LOOKBACK_DAYS = 21     # nur News der letzten N Tage
EVENT_HORIZON_DAYS = 100    # kommende Events bis N Tage


# ----------------------------- HTTP-Helfer -----------------------------
def http_get_json(url, tries=3, pause=1.5):
    """GET mit JSON-Antwort, einfache Retries."""
    for attempt in range(tries):
        try:
            req = request.Request(url, headers={"User-Agent": "research-dashboard/1.0"})
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code == 429:                      # rate limit -> warten
                time.sleep(pause * (attempt + 1) * 2)
                continue
            print(f"   HTTP {e.code} bei {url.split('?')[0]}")
            return None
        except Exception as e:
            if attempt == tries - 1:
                print(f"   Fehler bei {url.split('?')[0]}: {e}")
                return None
            time.sleep(pause)
    return None


def fmp(path, **params):
    params["apikey"] = FMP_KEY
    return http_get_json(f"{FMP_BASE}/{path}?{parse.urlencode(params)}")


# ----------------------------- FMP-Abrufe -----------------------------
def get_prices(symbol):
    """Taegliche Schlusskurse (aelteste zuerst) + Waehrung."""
    data = fmp(f"historical-price-full/{parse.quote(symbol)}",
               serietype="line", timeseries=HORIZON_DAYS)
    if not data or "historical" not in data:
        return None
    hist = data["historical"]
    closes = [float(h["close"]) for h in reversed(hist) if h.get("close") is not None]
    return {"closes": closes} if len(closes) >= 20 else None


def get_currency(symbol):
    q = fmp(f"quote/{parse.quote(symbol)}")
    if isinstance(q, list) and q:
        # FMP liefert keine Waehrung im quote; aus Profil holen
        prof = fmp(f"profile/{parse.quote(symbol)}")
        if isinstance(prof, list) and prof:
            return prof[0].get("currency", "") or ""
    return ""


def get_news(symbol):
    data = fmp("stock_news", tickers=symbol, limit=12)
    if not isinstance(data, list):
        return []
    cutoff = TODAY - dt.timedelta(days=NEWS_LOOKBACK_DAYS)
    out = []
    for n in data:
        date_str = (n.get("publishedDate") or "")[:10]
        try:
            d = dt.date.fromisoformat(date_str)
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append({
            "date": date_str,
            "title": (n.get("title") or "").strip(),
            "summary": (n.get("text") or "").strip()[:400],
            "source": n.get("site") or "",
            "url": n.get("url") or "",
            "category": "Sonstiges",
        })
    return out[:6]


def get_events(symbol):
    """Kommende Earnings-Termine (FMP earning_calendar je Symbol)."""
    frm = TODAY.isoformat()
    to = (TODAY + dt.timedelta(days=EVENT_HORIZON_DAYS)).isoformat()
    data = fmp("earning_calendar", symbol=symbol, **{"from": frm, "to": to})
    out = []
    if isinstance(data, list):
        for e in data:
            date_str = (e.get("date") or "")[:10]
            if date_str >= frm:
                out.append({"date": date_str, "title": "Earnings / Quartalszahlen", "type": "Earnings"})
    return out[:4]


# ----------------------------- Technische Analyse -----------------------------
def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def ema_series(a, n):
    if not a:
        return []
    k = 2 / (n + 1)
    out = [a[0]]
    for i in range(1, len(a)):
        out.append(a[i] * k + out[-1] * (1 - k))
    return out


def rsi(a, n=14):
    if len(a) < n + 1:
        return None
    g = l = 0.0
    for i in range(len(a) - n, len(a)):
        d = a[i] - a[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    if l == 0:
        return 100.0
    rs = (g / n) / (l / n)
    return 100 - 100 / (1 + rs)


def macd(a):
    if len(a) < 26:
        return None
    e12, e26 = ema_series(a, 12), ema_series(a, 26)
    line = [e12[i] - e26[i] for i in range(len(a))]
    sig = ema_series(line[25:], 9)
    return {"line": line[-1], "signal": sig[-1]}


def bollinger(a, n=20, k=2):
    if len(a) < n:
        return None
    s = a[-n:]
    m = sum(s) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in s) / n)
    return {"mid": m, "upper": m + k * sd, "lower": m - k * sd}


def compute_ta(closes, currency=""):
    if not closes or len(closes) < 20:
        return None
    price = closes[-1]
    s50, s200 = sma(closes, 50), sma(closes, 200)
    r, m, bb = rsi(closes), macd(closes), bollinger(closes)
    score, reasons = 0, []
    if s50 is not None:
        if price > s50: score += 1; reasons.append("Kurs ueber SMA50")
        else: score -= 1; reasons.append("Kurs unter SMA50")
    if s200 is not None:
        if price > s200: score += 1; reasons.append("Kurs ueber SMA200")
        else: score -= 1; reasons.append("Kurs unter SMA200")
    if m:
        if m["line"] > m["signal"]: score += 1; reasons.append("MACD ueber Signallinie")
        else: score -= 1; reasons.append("MACD unter Signallinie")
    if r is not None:
        if r > 70: score -= 1; reasons.append("RSI ueberkauft (>70)")
        elif r < 30: score += 1; reasons.append("RSI ueberverkauft (<30)")
    if bb:
        if price > bb["upper"]: score -= 1; reasons.append("ueber oberem Bollinger-Band")
        elif price < bb["lower"]: score += 1; reasons.append("unter unterem Bollinger-Band")
    label = "Bullisch" if score >= 2 else "Baerisch" if score <= -2 else "Neutral"
    return {
        "currency": currency, "closes": closes,
        "price": round(price, 2),
        "sma50": round(s50, 2) if s50 else None,
        "sma200": round(s200, 2) if s200 else None,
        "rsi": round(r) if r is not None else None,
        "macd": "bullisch" if m and m["line"] > m["signal"] else ("baerisch" if m else None),
        "high": round(max(closes), 2), "low": round(min(closes), 2),
        "score": score, "label": label, "reasons": reasons,
    }


# ----------------------------- Anthropic-Summary (optional) -----------------------------
def summarize_news(name, news):
    """Verdichtet Roh-News zu kurzen deutschen Summaries + Kategorie. Optional."""
    if not ANTHROPIC_KEY or not news:
        # Fallback: gekuerzte Originaltexte ohne KI
        for n in news:
            n["summary"] = (n["summary"] or "")[:220]
        return news
    items = [{"title": n["title"], "text": n["summary"][:300]} for n in news]
    prompt = (
        f'Fasse folgende Meldungen zum Unternehmen "{name}" jeweils in EINEM deutschen Satz '
        f'zusammen und ordne eine Kategorie zu (Earnings, Conference, Guidance, M&A, Rating, '
        f'Markt oder Sonstiges). Antworte NUR als JSON-Array in identischer Reihenfolge: '
        f'[{{"summary":"...","category":"..."}}]. Meldungen: {json.dumps(items, ensure_ascii=False)}'
    )
    try:
        body = json.dumps({
            "model": ANTHROPIC_MODEL,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"})
        with request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.replace("```json", "").replace("```", "").strip()
        arr = json.loads(text[text.index("["): text.rindex("]") + 1])
        for i, n in enumerate(news):
            if i < len(arr):
                n["summary"] = arr[i].get("summary", n["summary"])[:300]
                cat = arr[i].get("category", "Sonstiges")
                valid = {"Earnings", "Conference", "Guidance", "M&A", "Rating", "Markt", "Sonstiges"}
                n["category"] = cat if cat in valid else "Sonstiges"
    except Exception as e:
        print(f"   Summary-Fallback ({name}): {e}")
        for n in news:
            n["summary"] = (n["summary"] or "")[:220]
    return news


# ----------------------------- Hauptlauf -----------------------------
def main():
    if not FMP_KEY:
        print("FEHLER: FMP_API_KEY ist nicht gesetzt. Abbruch.")
        sys.exit(1)

    with open(COMPANIES_FILE, encoding="utf-8") as f:
        companies = json.load(f)

    snapshot = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "companies": {}, "news": [], "events": [],
    }

    for c in companies:
        cid, name, sym = c["id"], c["name"], c.get("symbol")
        print(f"-> {name} ({sym or 'kein Symbol'})")
        entry = {"news": [], "events": [], "tech": None,
                 "profile": {"businessModel": "", "differentiation": ""}}

        if sym:
            prices = get_prices(sym)
            if prices and not c.get("noChart"):
                entry["tech"] = compute_ta(prices["closes"], get_currency(sym))

            raw_news = get_news(sym)
            raw_news = summarize_news(name, raw_news)
            for n in raw_news:
                n.update({"companyId": cid, "company": name,
                          "asset": c["asset"], "status": c["status"]})
            entry["news"] = raw_news
            snapshot["news"].extend(raw_news)

            for e in get_events(sym):
                e.update({"companyId": cid, "company": name})
                entry["events"].append(e)
                snapshot["events"].append(e)

            time.sleep(0.3)   # FMP schonen

        snapshot["companies"][str(cid)] = entry

    snapshot["news"].sort(key=lambda n: n.get("date", ""), reverse=True)
    snapshot["events"].sort(key=lambda e: e.get("date", ""))

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)

    print(f"\nFertig: {len(snapshot['news'])} News, {len(snapshot['events'])} Events "
          f"-> {os.path.relpath(OUT_FILE, ROOT)}")


if __name__ == "__main__":
    main()
