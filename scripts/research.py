#!/usr/bin/env python3
"""
Research Dashboard — taeglicher Datensammler.

Laeuft im GitHub-Actions-Workflow (cron). Liest data/companies.json,
holt pro Titel Kurse (Financial Modeling Prep), News (Google-News-RSS),
Insider-Transaktionen (SEC/Wiener Boerse) und Earnings-Termine (Yahoo Finance,
inkl. der hinterlegten Wettbewerber), berechnet technische Indikatoren, fasst
optional via Anthropic-API zusammen und schreibt das Ergebnis nach
docs/data/snapshot.json (von GitHub Pages ausgeliefert).

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
import html as html_mod
import re
import threading
import http.cookiejar as cookiejar
from xml.etree import ElementTree as ET
from urllib import request, parse, error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPANIES_FILE = os.path.join(ROOT, "docs", "data", "companies.json")
OUT_FILE = os.path.join(ROOT, "docs", "data", "snapshot.json")

FMP_KEY = os.environ.get("FMP_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
FMP_BASE = "https://financialmodelingprep.com/stable"
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

TODAY = dt.date.today()
HORIZON_DAYS = 95           # Kurshistorie (Tage)
NEWS_LOOKBACK_DAYS = 21     # nur News der letzten N Tage
EVENT_HORIZON_DAYS = 100    # kommende Events bis N Tage

# Seriöse Quellen: Pressemitteilungs-Verteiler + Wirtschaftsjournalismus.
# Abgleich case-insensitiv NUR gegen den Quellennamen (nicht die URL).
PR_WIRES = (
    "globenewswire", "businesswire", "business wire", "prnewswire", "pr newswire",
    "newswire", "eqs-news", "eqs news", "dgap", "investegate", "regulatory news service",
    "presseportal", "globe newswire",
)
SOURCE_WHITELIST = PR_WIRES + (
    "financial times", "ft.com", "wall street journal", "wsj", "reuters", "bloomberg",
    "handelsblatt", "börsen-zeitung", "boersen-zeitung", "the economist", "economist",
    "cnbc", "marketwatch", "barron", "forbes", "fortune", "nikkei", "financial post",
    "globe and mail", "the times", "the guardian", "telegraph", "der standard", "die presse",
    "frankfurter allgemeine", "süddeutsche", "manager magazin",
    "wirtschaftswoche", "wirtschafts woche", "nzz", "neue zürcher", "les echos",
    "il sole 24 ore", "associated press", "ap news",
)
# Aktienportale / Boulevard / Konsum-Tech -> ausschliessen (gewinnt gegen Whitelist)
SOURCE_BLACKLIST = (
    "boerse", "börse", "finanzen.net", "finanzen.ch", "finanzen.at", "aktionär", "aktionaer",
    "wallstreet online", "wallstreet-online", "ad hoc", "ad-hoc", "adhoc", "finanznachrichten",
    "aktiencheck", "marketscreener", "simplywall", "simply wall", "fool", "tipranks",
    "stocktitan", "stocktwits", "benzinga", "zacks", "investorplace", "gurufocus", "wallmine",
    "marketbeat", "stockanalysis", "boersengefluester", "4investors", "onvista", "ariva",
    "investing.com", "reutersconnect", "chip", "netzwelt", "golem", "delamar",
    "computerbase", "heise", "winfuture", "mydealz", "digital fernsehen",
)

# Themenfilter: nur Unternehmens-/Finanzentwicklungen zulassen
FINANCE_KEYWORDS = (
    "earnings", "results", "ergebnis", "quartal", "halbjahr", "half-year", "full-year",
    "jahreszahlen", "umsatz", "revenue", "profit", "gewinn", "loss", "verlust", "ebit",
    "guidance", "ausblick", "prognose", "outlook", "forecast", "trading update",
    "dividend", "dividende", "buyback", "rückkauf", "rueckkauf", "share", "aktie", "notes",
    "bond", "anleihe", "refinanz", "rating", "downgrade", "upgrade", "moody", "fitch",
    "acquisition", "übernahme", "uebernahme", "merger", "fusion", "acquire", "stake",
    "beteiligung", "divest", "joint venture", "investment decision", "investitionsentscheidung",
    "ceo", "cfo", "chair", "vorstand", "appoint", "ernennt", "steps down", "rücktritt",
    "contract", "auftrag", "order", "wins", "launch", "restructur", "restrukturier",
    "layoff", "stellenabbau", "stellen", "insolven", "profit warning", "gewinnwarnung",
    "capital markets day", "investor day", "hauptversammlung", "agm", "annual general meeting",
    "conference", "konferenz", "presents at", "to report", "expansion", "ausbau", "fid",
)


# ----------------------------- HTTP-Helfer -----------------------------
def http_get_json(url, tries=2, pause=1.0):
    """GET mit JSON-Antwort, knappe Retries (keine langen Haenger)."""
    for attempt in range(tries):
        try:
            req = request.Request(url, headers={"User-Agent": "research-dashboard/1.0"})
            with request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:   # rate limit -> kurz warten
                time.sleep(2)
                continue
            if e.code not in (429,):
                print(f"   HTTP {e.code} bei {url.split('?')[0]}")
            return None
        except Exception as e:
            if attempt == tries - 1:
                print(f"   Fehler bei {url.split('?')[0]}: {e}")
                return None
            time.sleep(pause)
    return None


def http_get_text(url, tries=2, pause=0.8):
    """GET mit Text-Antwort (fuer RSS/XML)."""
    for attempt in range(tries):
        try:
            req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 research-dashboard/1.0"})
            with request.urlopen(req, timeout=12) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:
            if attempt == tries - 1:
                print(f"   RSS-Fehler bei {url.split('?')[0]}: {e}")
                return None
            time.sleep(pause)
    return None


def fmp(path, **params):
    params["apikey"] = FMP_KEY
    return http_get_json(f"{FMP_BASE}/{path}?{parse.urlencode(params)}")


# ----------------------------- FMP-Abrufe (stable API) -----------------------------
def get_prices(symbol):
    """Taegliche Schlusskurse (aelteste zuerst). Erst 'full', bei Sperre 'light'."""
    frm = (TODAY - dt.timedelta(days=HORIZON_DAYS * 2)).isoformat()
    to = TODAY.isoformat()
    for path, field in (("historical-price-eod/full", "close"),
                        ("historical-price-eod/light", "price")):
        data = fmp(path, symbol=symbol, **{"from": frm, "to": to})
        if isinstance(data, list) and data:
            rows = [d for d in data if d.get(field) is not None and d.get("date")]
            rows.sort(key=lambda d: d["date"])
            closes = [float(d[field]) for d in rows][-HORIZON_DAYS:]
            if len(closes) >= 20:
                return {"closes": closes}
    return None


def get_currency(symbol):
    prof = fmp("profile", symbol=symbol)
    if isinstance(prof, list) and prof:
        return prof[0].get("currency", "") or ""
    return ""


# ----------------------------- Yahoo Finance (Earnings-Termine) -----------------------------
# Nutzt den (inoffiziellen) Yahoo-Finance-JSON-Endpunkt fuer kommende Earnings.
# Vorteil ggue. FMP-Free: deckt internationale Boersen ab (.L/.DE/.PA/.AS/.ST/.AX ...).
# Reines urllib, KEINE Zusatzpakete. Faellt ein Aufruf aus -> leer (leer ist besser als falsch).
_YA_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 research-dashboard/1.0"}


def yahoo_session():
    """Holt einmal pro Lauf Cookie + Crumb. Gibt (opener, crumb|None) zurueck."""
    cj = cookiejar.CookieJar()
    op = request.build_opener(request.HTTPCookieProcessor(cj))

    def _g(url):
        req = request.Request(url, headers=_YA_UA)
        with op.open(req, timeout=15) as r:
            return r.read().decode("utf-8", "replace")

    try:
        _g("https://fc.yahoo.com")          # setzt das noetige Cookie
    except Exception:
        pass
    try:
        crumb = _g("https://query2.finance.yahoo.com/v1/test/getcrumb").strip()
    except Exception:
        crumb = None
    return op, (crumb or None)


def _yahoo_get(op, url, tries=2):
    for attempt in range(tries):
        try:
            req = request.Request(url, headers=_YA_UA)
            with op.open(req, timeout=15) as r:
                return r.read().decode("utf-8", "replace")
        except error.HTTPError as e:
            if e.code == 429 and attempt < tries - 1:    # Rate-Limit -> kurz warten
                time.sleep(2.0)
                continue
            return None
        except Exception:
            if attempt == tries - 1:
                return None
            time.sleep(0.6)
    return None


def yahoo_next_earnings(op, crumb, symbol):
    """Kommende Earnings-Termine eines Symbols als ISO-Datums-Liste (kann leer sein)."""
    if not symbol or not crumb:
        return []
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{parse.quote(symbol)}"
           f"?modules=calendarEvents&crumb={parse.quote(crumb)}")
    raw = _yahoo_get(op, url)
    if not raw:
        return []
    try:
        res = json.loads(raw).get("quoteSummary", {}).get("result") or []
        if not res:
            return []
        dates = res[0].get("calendarEvents", {}).get("earnings", {}).get("earningsDate", [])
        out = []
        for d in dates:
            ts = d.get("raw")
            if ts:
                out.append(dt.datetime.utcfromtimestamp(ts).date().isoformat())
        return sorted(set(out))
    except Exception:
        return []


def yahoo_resolve_symbol(op, name):
    """Wettbewerber-Name -> bestes Yahoo-Symbol (oder None). Bevorzugt echte Aktien."""
    q = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()      # Klammerzusatz weg ("Nu Holdings (Nubank)")
    if not q:
        return None
    url = (f"https://query2.finance.yahoo.com/v1/finance/search?q={parse.quote(q)}"
           f"&quotesCount=5&newsCount=0")
    raw = _yahoo_get(op, url)
    if not raw:
        return None
    try:
        quotes = json.loads(raw).get("quotes", [])
    except Exception:
        return None
    for qd in quotes:
        if qd.get("quoteType") == "EQUITY" and qd.get("symbol"):
            return qd["symbol"]
    for qd in quotes:
        if qd.get("symbol"):
            return qd["symbol"]
    return None


def _relevant(name, title, summary):
    """Behalte nur Meldungen, die WIRKLICH dieses Unternehmen betreffen:
    voller Name als Phrase ODER alle markanten Namensbestandteile vorhanden."""
    hay = (title + " " + summary).lower()
    nm = name.lower()
    if nm in hay:
        return True
    words = [w for w in re.split(r"[^a-zA-ZäöüÄÖÜ0-9]+", nm) if len(w) >= 3]
    if not words:
        return False
    return all(w in hay for w in words)


def _source_ok(source):
    """Nur Pressemitteilungen + serioese Wirtschaftsmedien; Aktienportale/Konsum raus.
    Abgleich ausschliesslich gegen den Quellennamen."""
    s = source.lower().strip()
    if not s:
        return False
    if any(b in s for b in SOURCE_BLACKLIST):
        return False
    return any(w in s for w in SOURCE_WHITELIST)


def _is_pr(source):
    return any(w in source.lower() for w in PR_WIRES)


def _topical(title, summary, source):
    """Nur Unternehmens-/Finanzentwicklungen. PR-Wires gelten immer als relevant."""
    if _is_pr(source):
        return True
    hay = (title + " " + summary).lower()
    return any(k in hay for k in FINANCE_KEYWORDS)


INSIDER_HEADLINE = ("pdmr", "director/pdmr", "directors’ dealings", "directors' dealings",
                    "managers’ transactions", "managers' transactions", "manager's transaction",
                    "persons discharging managerial", "directors dealings")


def _categorize(title, summary, source):
    hay = (title + " " + summary).lower()
    if any(k in hay for k in INSIDER_HEADLINE):
        return "Insider"
    groups = [
        ("Earnings", ("earnings", "results", "ergebnis", "quartal", "halbjahr", "half-year",
                      "full-year", "jahreszahlen", "umsatz", "revenue", "profit", "gewinn",
                      "loss", "verlust", "ebit", "q1", "q2", "q3", "q4", "fy")),
        ("Guidance", ("guidance", "ausblick", "prognose", "outlook", "forecast", "trading update",
                      "profit warning", "gewinnwarnung", "raises", "cuts", "delays")),
        ("M&A", ("acquisition", "übernahme", "uebernahme", "merger", "fusion", "acquire", "stake",
                 "beteiligung", "divest", "joint venture", "investment decision",
                 "investitionsentscheidung", "expansion", "ausbau", "fid")),
        ("Conference", ("conference", "konferenz", "capital markets day", "investor day",
                        "hauptversammlung", "annual general meeting", "agm", "presents at")),
        ("Rating", ("rating", "downgrade", "upgrade", "moody", "fitch", "creditwatch", "bond",
                    "anleihe", "notes", "refinanz")),
    ]
    for cat, kws in groups:
        if any(k in hay for k in kws):
            return cat
    return "Sonstiges" if _is_pr(source) else "Markt"


def _fetch_rss(name, query, lang):
    if lang == "de":
        loc = "&hl=de&gl=DE&ceid=DE:de"
    else:
        loc = "&hl=en-US&gl=US&ceid=US:en"
    url = "https://news.google.com/rss/search?q=" + parse.quote(query + " when:30d") + loc
    xml = http_get_text(url)
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    from email.utils import parsedate_to_datetime
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = item.findtext("description") or ""
        try:
            date_iso = parsedate_to_datetime(pub).date().isoformat()
        except Exception:
            date_iso = TODAY.isoformat()
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)
        summary = re.sub(r"<[^>]+>", " ", html_mod.unescape(desc))
        summary = re.sub(r"\s+", " ", summary).strip()[:240]
        title = title.strip()
        source = source.strip()
        if not _relevant(name, title, summary):
            continue
        if not _source_ok(source):
            continue
        if not _topical(title, summary, source):
            continue
        out.append({"date": date_iso, "title": title,
                    "summary": summary or title,
                    "source": source, "url": link,
                    "category": _categorize(title, summary, source)})
    return out


def get_news(name, query=None):
    """Kostenlose News via Google-News-RSS, deutsch + englisch zusammengefuehrt.
    Gefiltert auf Pressemitteilungen + serioese Wirtschaftsmedien (keine Aktienportale,
    kein Konsum-/Boulevard-Rauschen), thematisch auf Unternehmens-/Finanzentwicklungen."""
    q = query or f'"{name}"'
    items, seen = [], set()
    for lang in ("de", "en"):
        for n in _fetch_rss(name, q, lang):
            key = n["title"].lower()
            if key not in seen:
                seen.add(key)
                items.append(n)
    items.sort(key=lambda n: n["date"], reverse=True)
    return items[:6]


def anthropic_text(prompt, max_tokens=1024):
    """Ruft die Anthropic-API auf und gibt den Text zurueck (oder None)."""
    if not ANTHROPIC_KEY:
        return None
    try:
        body = json.dumps({
            "model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01"})
        with request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    except Exception as e:
        print(f"   Anthropic-Fehler: {e}")
        return None


def get_ir_events(url, name):
    """Laedt die IR-Kalenderseite und laesst Claude die kommenden Termine extrahieren.
    Benoetigt ANTHROPIC_API_KEY. Bei JS-gerenderten Seiten kann das Ergebnis leer sein."""
    if not url or not ANTHROPIC_KEY:
        return []
    html = http_get_text(url)
    if not html:
        return []
    # HTML grob zu Text reduzieren
    text = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html_mod.unescape(text)).strip()
    # Budget auf den Bereich mit Datumsangaben zentrieren (statt Seitenanfang)
    if len(text) > 7000:
        m = re.search(r"(20(2[6-9])|\d{1,2}[./]\d{1,2}[./]20\d{2})", text)
        if m:
            start = max(0, m.start() - 600)
            text = text[start:start + 7000]
        else:
            text = text[:7000]
    horizon = (TODAY + dt.timedelta(days=EVENT_HORIZON_DAYS)).isoformat()
    prompt = (
        f'Aus dem folgenden Text der Investor-Relations-/Finanzkalender-Seite von "{name}" '
        f'extrahiere die KOMMENDEN Termine (heute {TODAY.isoformat()} bis {horizon}). '
        f'Antworte NUR mit gueltigem JSON-Array, kein Markdown: '
        f'[{{"date":"YYYY-MM-DD","title":"kurzer Titel","type":"Earnings|Conference|Hauptversammlung|Capital Markets Day|Sonstiges"}}]. '
        f'Nur Termine mit konkretem Datum, nur in der Zukunft. Wenn keine erkennbar: []. '
        f'Text: {text}'
    )
    raw = anthropic_text(prompt, max_tokens=800)
    if not raw:
        return []
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        arr = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
    except Exception:
        return []
    out = []
    valid_types = {"Earnings", "Conference", "Hauptversammlung", "Capital Markets Day", "Sonstiges"}
    for e in arr:
        d = str(e.get("date", ""))[:10]
        try:
            if not (TODAY.isoformat() <= d <= horizon):
                continue
        except Exception:
            continue
        t = e.get("type", "Sonstiges")
        out.append({"date": d, "title": str(e.get("title", "Termin"))[:120],
                    "type": t if t in valid_types else "Sonstiges"})
    out.sort(key=lambda x: x["date"])
    return out[:8]


# ----------------------------- Insider-Transaktionen (SEC EDGAR, US-Titel) -----------------------------
SEC_UA = os.environ.get("SEC_USER_AGENT", "research-dashboard insider-monitor example@example.com")
_SEC_MAP = None
_SEC_LOCK = threading.Lock()


def _sec_get(url):
    try:
        req = request.Request(url, headers={"User-Agent": SEC_UA})
        with request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"   SEC-Fehler ({url.rsplit('/', 1)[-1]}): {e}")
        return None


def _sec_cik(symbol):
    """Ticker -> CIK (nur US-Ticker ohne Boersen-Suffix). None, wenn kein SEC-Filer."""
    global _SEC_MAP
    if not symbol or "." in symbol:
        return None
    if _SEC_MAP is None:
        with _SEC_LOCK:
            if _SEC_MAP is None:
                raw = _sec_get("https://www.sec.gov/files/company_tickers.json")
                m = {}
                if raw:
                    try:
                        for v in json.loads(raw).values():
                            m[v["ticker"].upper()] = str(v["cik_str"]).zfill(10)
                    except Exception:
                        pass
                _SEC_MAP = m
    return _SEC_MAP.get(symbol.upper())


def _parse_form4(xml, cik_plain, accnd):
    try:
        root = ET.fromstring(xml)
    except Exception:
        return []
    name = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    isDir = (root.findtext(".//reportingOwnerRelationship/isDirector") or "0").strip().lower()
    isTen = (root.findtext(".//reportingOwnerRelationship/isTenPercentOwner") or "0").strip().lower()
    title = (root.findtext(".//reportingOwnerRelationship/officerTitle") or "").strip()
    role = title or ("Director" if isDir in ("1", "true") else "") \
        or ("10%-Eigner" if isTen in ("1", "true") else "") or "Insider"
    folder = f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{accnd}/"

    def val(p, tag):
        e = p.find(tag)
        if e is None:
            return ""
        v = e.find("value")
        return ((v.text if v is not None else e.text) or "").strip()

    res = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = val(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):          # nur Open-Market Kauf (P) / Verkauf (S)
            continue
        try:
            sh = float(val(t, "transactionAmounts/transactionShares") or 0)
            pr = float(val(t, "transactionAmounts/transactionPricePerShare") or 0)
        except ValueError:
            sh, pr = 0.0, 0.0
        res.append({
            "date": val(t, "transactionDate")[:10], "insider": name, "role": role,
            "code": code, "kind": "Kauf" if code == "P" else "Verkauf",
            "shares": int(sh), "price": round(pr, 2) if pr else None,
            "value": int(sh * pr) if sh and pr else None, "url": folder, "source": "SEC",
        })
    return res


def get_insider_tx(symbol, lookback_days=75, max_filings=15):
    """Juengste Open-Market-Insidertransaktionen aus SEC-Form-4-Filings (nur US-Titel)."""
    cik = _sec_cik(symbol)
    if not cik:
        return []
    sub = _sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if not sub:
        return []
    try:
        rec = json.loads(sub)["filings"]["recent"]
    except Exception:
        return []
    cik_plain = str(int(cik))
    cutoff = (TODAY - dt.timedelta(days=lookback_days)).isoformat()
    forms = rec.get("form", []); dates = rec.get("filingDate", [])
    accs = rec.get("accessionNumber", []); docs = rec.get("primaryDocument", [])
    out = []; count = 0
    for i, f in enumerate(forms):
        if dates[i] < cutoff:               # "recent" ist neueste zuerst
            break
        if f != "4":
            continue
        if count >= max_filings:
            break
        count += 1
        doc = re.sub(r"^xsl[^/]*/", "", docs[i])     # XSL-Render-Prefix entfernen -> Roh-XML
        if not doc.lower().endswith(".xml"):
            continue
        accnd = accs[i].replace("-", "")
        xml = _sec_get(f"https://www.sec.gov/Archives/edgar/data/{cik_plain}/{accnd}/{doc}")
        if xml:
            out.extend(_parse_form4(xml, cik_plain, accnd))
        time.sleep(0.12)                     # EDGAR hoeflich behandeln
    out.sort(key=lambda o: o["date"], reverse=True)
    return out[:15]


# ----------------------------- Insider-Transaktionen (Wiener Boerse PDF, AT-Titel) -----------------------------
_AT_TEXT = None
_AT_LOCK = threading.Lock()
_AT_ROLE_HINTS = ("Chief Executive Officer", "Chief Financial Officer", "Mitglied des Vorstands",
                  "Vorsitzender des Vorstands", "Vorstand", "Aufsichtsrat", "Director",
                  "CEO", "CFO", "President")


def _at_pdf_text():
    """Laedt das jaehrliche 'Directors' Dealings'-PDF der Wiener Boerse einmal pro Lauf."""
    global _AT_TEXT
    if _AT_TEXT is None:
        with _AT_LOCK:
            if _AT_TEXT is None:
                _AT_TEXT = ""
                url = ("https://www.wienerborse.at/uploads/u/cms/files/marktdaten/statistiken/"
                       f"directors-dealings-{TODAY.year}.pdf")
                try:
                    from pypdf import PdfReader
                    import io
                    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0 research-dashboard"})
                    with request.urlopen(req, timeout=30) as resp:
                        raw = resp.read()
                    reader = PdfReader(io.BytesIO(raw))
                    _AT_TEXT = "\n".join((p.extract_text() or "") for p in reader.pages)
                except Exception as e:
                    print(f"   Wiener-Boerse-PDF-Fehler: {e}")
                    _AT_TEXT = ""
    return _AT_TEXT


def get_austria_insider(isin, lookback_days=150):
    """Open-Market-Insidertransaktionen (Kauf/Verkauf) aus dem Wiener-Boerse-PDF fuer EINE ISIN."""
    text = _at_pdf_text()
    if not text or not isin:
        return []
    cutoff = TODAY - dt.timedelta(days=lookback_days)
    out = []
    for line in text.splitlines():
        if isin not in line:
            continue
        right = line.split(isin, 1)[1]
        m = re.search(r"(\d{2}\.\d{2}\.\d{4})", right)
        if not m:
            continue
        place = right[m.end():].strip()
        if "Außerhalb" in place or "Ausserhalb" in place:   # nur On-Market
            continue
        pre = right[:m.start()]
        kind = "Kauf" if re.search(r"Erwerb|Kauf", pre) else ("Verkauf" if re.search(r"Veräußerung|Verkauf", pre) else None)
        if not kind:
            continue
        pm = re.search(r"(\d{1,3}(?:\.\d{3})*,\d+)", pre)        # Preis (europ. Format)
        if not pm:
            continue
        try:
            price = float(pm.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            continue
        if price <= 0:
            continue
        rest = pre[pm.end():]
        vm = re.search(r"((?:\d[\d\s]*)?\d)", rest.replace("EUR", " "))   # Volumen (Leerz. als Tausender)
        try:
            shares = int(vm.group(1).replace(" ", "")) if vm else 0
        except ValueError:
            shares = 0
        try:
            d = dt.date(*[int(x) for x in reversed(m.group(1).split("."))])
        except Exception:
            continue
        if d < cutoff:
            continue
        left = line.split(isin, 1)[0].strip()
        role = next((h for h in _AT_ROLE_HINTS if h in left), "Insider")
        parts = left.split(role) if role in left else [left]
        name = (parts[0].strip() or (parts[1].strip() if len(parts) > 1 else "")) or " ".join(left.split()[:2])
        out.append({"date": d.isoformat(), "insider": name or "—", "role": role,
                    "code": "P" if kind == "Kauf" else "S", "kind": kind,
                    "shares": shares, "price": round(price, 2),
                    "value": int(shares * price) if shares else None,
                    "url": ("https://www.wienerborse.at/uploads/u/cms/files/marktdaten/statistiken/"
                            f"directors-dealings-{TODAY.year}.pdf"), "source": "Wiener Börse"})
    out.sort(key=lambda o: o["date"], reverse=True)
    return out[:15]


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
    text = anthropic_text(prompt, max_tokens=1024)
    if not text:
        for n in news:
            n["summary"] = (n["summary"] or "")[:220]
        return news
    try:
        text = text.replace("```json", "").replace("```", "").strip()
        arr = json.loads(text[text.index("["): text.rindex("]") + 1])
        valid = {"Earnings", "Conference", "Guidance", "M&A", "Rating", "Markt", "Sonstiges"}
        for i, n in enumerate(news):
            if i < len(arr):
                n["summary"] = arr[i].get("summary", n["summary"])[:300]
                if n.get("category") != "Insider":      # Insider-Klassifizierung nicht ueberschreiben
                    cat = arr[i].get("category", "Sonstiges")
                    n["category"] = cat if cat in valid else n["category"]
    except Exception as e:
        print(f"   Summary-Fallback ({name}): {e}")
        for n in news:
            n["summary"] = (n["summary"] or "")[:220]
    return news


# ----------------------------- Hauptlauf -----------------------------
def _news_key(n):
    """Stabiler Schluessel zum Wiedererkennen einer Meldung zwischen Laeufen."""
    return n.get("url") or (str(n.get("companyId", "")) + "|" + n.get("title", ""))


def _event_key(e):
    """Stabiler Schluessel fuer Events (Titel/Tag/Typ/angezeigter Name -> Wettbewerber kollidieren nicht)."""
    return (f'{e.get("companyId", "")}|{(e.get("date") or "")[:10]}|'
            f'{e.get("type", "")}|{e.get("company", "")}')


def _insider_key(x):
    """Stabiler Schluessel fuer eine Insider-Transaktion zwischen Laeufen."""
    return x.get("url") or "|".join(str(x.get(k, "")) for k in ("date", "insider", "shares", "code"))


def process_company(c, prev_entry=None, run_stamp=None, refresh_events=True):
    """Sammelt News, Kurse und IR-Events fuer EINEN Titel.
    Inkrementell: bereits bekannte News werden 1:1 uebernommen (keine erneute KI-Summary);
    nur WIRKLICH NEUE Meldungen gehen an Claude und werden mit firstSeen=run_stamp markiert."""
    cid, name, sym = c["id"], c["name"], c.get("symbol")
    entry = {"news": [], "events": [], "insider": [], "tech": None,
             "profile": (prev_entry or {}).get("profile", {"businessModel": "", "differentiation": ""})}

    # Bekannte News aus dem letzten Snapshot indizieren
    prev_news = {(_news_key(n)): n for n in (prev_entry or {}).get("news", [])}

    fetched = get_news(name, c.get("newsQuery"))
    known, fresh = [], []
    for n in fetched:
        k = _news_key(n)
        if k in prev_news:
            known.append(prev_news[k])          # schon zusammengefasst -> unveraendert uebernehmen
        else:
            fresh.append(n)                      # neu -> gleich zusammenfassen

    if fresh:
        fresh = summarize_news(name, fresh)
        for n in fresh:
            n["firstSeen"] = run_stamp           # markiert die Meldung als "neu in diesem Lauf"
    for n in (known + fresh):
        n.update({"companyId": cid, "company": name, "asset": c["asset"], "status": c["status"]})
    # neueste zuerst, auf 8 begrenzen
    alln = sorted(known + fresh, key=lambda n: n.get("date", ""), reverse=True)[:8]
    entry["news"] = alln

    # Kurse via FMP (nur mit Symbol)
    if sym:
        prices = get_prices(sym)
        if prices and not c.get("noChart"):
            entry["tech"] = compute_ta(prices["closes"], get_currency(sym))

    # Insider-Transaktionen — Open-Market Kauf/Verkauf
    if c.get("atInsiderIsin"):
        ins = get_austria_insider(c["atInsiderIsin"])   # Wiener Boerse (AT-Titel)
    elif sym:
        ins = get_insider_tx(sym)                        # SEC EDGAR (US-Titel)
    else:
        ins = []
    # firstSeen: bereits bekannte Transaktionen behalten ihren Stempel, neue bekommen run_stamp
    prev_ins = {_insider_key(x): x for x in (prev_entry or {}).get("insider", [])}
    for x in ins:
        k = _insider_key(x)
        x["firstSeen"] = (prev_ins[k].get("firstSeen") if (k in prev_ins and prev_ins[k].get("firstSeen"))
                          else run_stamp)
    entry["insider"] = ins

    # Events: nur optionale IR-Termine von der Unternehmens-IR-Seite (falls URL hinterlegt).
    # Earnings (eigene Titel + Wettbewerber) holt zentral die Yahoo-Phase in main(); firstSeen
    # fuer ALLE Events wird ebenfalls dort gesetzt. Nur am woechentlichen Refresh-Tag abrufen.
    if refresh_events and c.get("irCalendarUrl"):
        for e in get_ir_events(c.get("irCalendarUrl"), name):
            e.update({"companyId": cid, "company": name, "source": "IR-Seite", "peer": False})
            entry["events"].append(e)

    print(f"   fertig: {name} — {len(alln)} News ({len(fresh)} neu), "
          f"{len(entry['insider'])} Insider-Tx, "
          f"{'Kurs' if entry['tech'] else 'kein Kurs'}")
    return cid, entry


def main():
    if not FMP_KEY:
        print("FEHLER: FMP_API_KEY ist nicht gesetzt. Abbruch.")
        sys.exit(1)

    with open(COMPANIES_FILE, encoding="utf-8") as f:
        companies = json.load(f)

    # Vorherigen Snapshot laden (fuer inkrementelle News + Wettbewerber-Symbol-Cache)
    prev, prev_peer, prev_ev_refreshed = {}, {}, None
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            _prev_full = json.load(f)
        prev = _prev_full.get("companies", {})
        prev_peer = _prev_full.get("peerSymbols", {})
        prev_ev_refreshed = _prev_full.get("eventsRefreshedAt")
    except Exception:
        prev, prev_peer, prev_ev_refreshed = {}, {}, None

    # Events (Earnings-Kalender) nur EINMAL pro Woche aktualisieren — montags frueh, plus
    # Erst-Befuellung und ein Sicherheitsnetz, falls >7 Tage kein Refresh gelang. An allen
    # anderen Tagen werden die Termine aus dem vorherigen Snapshot uebernommen (schont Yahoo).
    _last_ev_date = None
    try:
        _last_ev_date = dt.date.fromisoformat(str(prev_ev_refreshed)[:10]) if prev_ev_refreshed else None
    except Exception:
        _last_ev_date = None
    _days_since = (TODAY - _last_ev_date).days if _last_ev_date else 9999
    refresh_events = (_last_ev_date is None) or (_days_since >= 7) or \
                     (TODAY.weekday() == 0 and _last_ev_date != TODAY)   # weekday 0 = Montag

    run_stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    snapshot = {"generatedAt": run_stamp, "companies": {}, "news": [], "events": [], "insider": []}

    from concurrent.futures import ThreadPoolExecutor
    print(f"Verarbeite {len(companies)} Titel parallel (inkrementell) …")
    print(f"Event-Kalender: {'AKTUALISIEREN (woechentlich/montags)' if refresh_events else 'aus Vorlauf uebernehmen'}.")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(
            lambda c: process_company(c, prev.get(str(c["id"])), run_stamp, refresh_events), companies))

    if refresh_events:
        # ---------- Earnings-Termine via Yahoo: eigene Titel + festgelegte Wettbewerber ----------
        today_iso = TODAY.isoformat()
        horizon_iso = (TODAY + dt.timedelta(days=EVENT_HORIZON_DAYS)).isoformat()
        entries = {cid: entry for cid, entry in results}
        op, crumb = yahoo_session()
        if not crumb:
            print("HINWEIS: Kein Yahoo-Crumb erhalten — Earnings-Termine werden uebersprungen "
                  "(Yahoo evtl. temporaer ratenbegrenzt). Naechster Lauf versucht es erneut.")

        # 1) Wettbewerber-Namen -> Symbole aufloesen (Cache aus dem vorherigen Snapshot weiterverwenden)
        peer_syms = dict(prev_peer or {})
        if crumb:
            new_resolved = 0
            for c in companies:
                for nm in (c.get("competitors") or []):
                    if nm and nm not in peer_syms:
                        sym = yahoo_resolve_symbol(op, nm)
                        if sym:
                            peer_syms[nm] = sym
                            new_resolved += 1
                        time.sleep(0.25)
            if new_resolved:
                print(f"   Wettbewerber-Symbole neu aufgeloest: {new_resolved}")

        # 2) Benoetigte Symbole sammeln (dedupliziert) -> jedes Symbol nur EINMAL abfragen
        need = {}   # symbol -> Liste von (cid, Anzeigename, is_peer, peer_of)
        for c in companies:
            cid, nm, sym = c["id"], c["name"], c.get("symbol")
            if sym:
                need.setdefault(sym, []).append((cid, nm, False, None))
            for pn in (c.get("competitors") or []):
                psym = peer_syms.get(pn)
                if psym:
                    need.setdefault(psym, []).append((cid, pn, True, nm))

        # 3) Earnings je Symbol holen (sequentiell + sanftes Pacing -> schont Yahoo)
        earnings_cache = {}
        if crumb:
            for sym in need:
                earnings_cache[sym] = yahoo_next_earnings(op, crumb, sym)
                time.sleep(0.25)
            got = sum(1 for v in earnings_cache.values() if v)
            print(f"   Earnings abgefragt: {len(need)} Symbole, davon {got} mit Termin.")

        # 4) Events bauen und an die jeweiligen Eintraege haengen
        for sym, attribs in need.items():
            for ds in earnings_cache.get(sym, []):
                if not (today_iso <= ds <= horizon_iso):
                    continue
                for cid, disp, is_peer, peer_of in attribs:
                    ent = entries.get(cid)
                    if ent is None:
                        continue
                    ev = {"date": ds, "type": "Earnings",
                          "title": f"{disp}: Quartalszahlen" + (" (Wettbewerber)" if is_peer else ""),
                          "company": disp, "companyId": cid,
                          "source": "Yahoo Finance", "peer": is_peer}
                    if is_peer:
                        ev["peerOf"] = peer_of
                    ent["events"].append(ev)

        # 5) firstSeen fuer ALLE Events je Titel setzen (inkrementell ggue. Vorlauf) + Dedupe + Sortierung
        for cid, entry in results:
            prev_ev = {_event_key(e): e for e in (prev.get(str(cid), {}) or {}).get("events", [])}
            seen = {}
            for e in entry["events"]:
                k = _event_key(e)
                if k in seen:
                    continue
                e["firstSeen"] = (prev_ev[k].get("firstSeen") if (k in prev_ev and prev_ev[k].get("firstSeen"))
                                  else run_stamp)
                seen[k] = e
            entry["events"] = sorted(seen.values(), key=lambda e: e.get("date", ""))

        # Crumb erhalten = Refresh galt als erfolgreich -> Wochenstempel setzen; sonst Vorlauf-Stempel behalten
        snapshot["eventsRefreshedAt"] = run_stamp if crumb else prev_ev_refreshed
        snapshot["peerSymbols"] = peer_syms
    else:
        # Kein Refresh-Tag: Events 1:1 aus dem vorherigen Snapshot uebernehmen (firstSeen bleibt erhalten)
        for cid, entry in results:
            entry["events"] = (prev.get(str(cid), {}) or {}).get("events", [])
        snapshot["eventsRefreshedAt"] = prev_ev_refreshed
        snapshot["peerSymbols"] = prev_peer

    new_count = 0
    for cid, entry in results:
        snapshot["companies"][str(cid)] = entry
        snapshot["news"].extend(entry["news"])
        snapshot["events"].extend(entry["events"])
        comp = next((c for c in companies if c["id"] == cid), {})
        for tx in entry.get("insider", []):
            snapshot["insider"].append({**tx, "companyId": cid, "company": comp.get("name", ""),
                                        "asset": comp.get("asset", ""), "status": comp.get("status", "")})
        new_count += sum(1 for n in entry["news"] if n.get("firstSeen") == run_stamp)

    snapshot["news"].sort(key=lambda n: n.get("date", ""), reverse=True)
    snapshot["events"].sort(key=lambda e: e.get("date", ""))
    snapshot["insider"].sort(key=lambda x: x.get("date", ""), reverse=True)

    new_ev = sum(1 for e in snapshot["events"] if e.get("firstSeen") == run_stamp)
    new_ins = sum(1 for x in snapshot["insider"] if x.get("firstSeen") == run_stamp)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)

    print(f"\nFertig: {len(snapshot['news'])} News ({new_count} neu), "
          f"{len(snapshot['events'])} Events ({new_ev} neu), "
          f"{len(snapshot['insider'])} Insider-Tx ({new_ins} neu) "
          f"-> {os.path.relpath(OUT_FILE, ROOT)}")

    have_tech = sum(1 for e in snapshot["companies"].values() if e.get("tech"))
    print(f"Kursdaten vorhanden fuer {have_tech} Titel.")
    if len(snapshot["news"]) == 0 and have_tech == 0:
        print("HINWEIS: Es kamen keinerlei Daten zurueck. Pruefe (1) ob FMP_API_KEY gueltig ist "
              "und (2) ob dein FMP-Plan die Endpunkte abdeckt. News/Earnings koennen je nach Plan "
              "eingeschraenkt sein; Kurse sind im Free-Plan i.d.R. verfuegbar.")


if __name__ == "__main__":
    main()
