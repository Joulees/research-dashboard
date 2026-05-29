# Research Dashboard

Ein selbst-gehostetes Research-Dashboard für Portfolio- und Watchlist-Unternehmen:
Newsflow, Event-Kalender, Märkte-Übersicht, Einzeltitel-Deep-Dive und technische Analyse.

Die Daten werden **einmal täglich um 07:00 automatisch** über GitHub Actions gesammelt
(Kurse, News, Earnings-Termine von Financial Modeling Prep) und als `snapshot.json`
abgelegt. Das Frontend (eine einzige HTML-Datei) liest nur diesen Snapshot — schnell,
kostenlos, ohne Server.

```
GitHub Actions (cron 07:00)  ──►  scripts/research.py  ──►  docs/data/snapshot.json
                                        │                          │
                                   FMP + (optional) Anthropic      ▼
                                                            GitHub Pages  ──►  docs/index.html
```

---

## Was du brauchst

1. Einen **GitHub-Account** (hast du).
2. Einen **kostenlosen FMP-API-Key**: https://site.financialmodelingprep.com → registrieren → Dashboard → API-Key kopieren. Der Free-Plan erlaubt 250 Anfragen/Tag (genug für ~37 Titel).
3. *(Optional)* Einen **Anthropic-API-Key** für KI-Zusammenfassungen der News. Ohne diesen Key werden die Original-Kurztexte verwendet.

---

## Einrichtung in 6 Schritten (ca. 20 Minuten, kein Programmieren)

### 1. Repo anlegen
- Auf GitHub oben rechts **+ → New repository**.
- Name z. B. `research-dashboard`, **Public** wählen (dann sind GitHub Actions unbegrenzt kostenlos und GitHub Pages frei verfügbar).
- **Create repository**.

### 2. Dateien hochladen
- Im neuen Repo: **Add file → Upload files**.
- Ziehe den kompletten Inhalt dieses Ordners hinein (die Struktur muss erhalten bleiben):
  ```
  .github/workflows/daily.yml
  docs/index.html
  docs/data/companies.json
  docs/data/snapshot.json
  scripts/research.py
  requirements.txt
  README.md
  ```
- **Commit changes**.

> Tipp: Wenn das Web-Upload die Ordner nicht anlegt, lade die Dateien einzeln hoch und gib im Dateinamen den Pfad mit an (z. B. `scripts/research.py`). GitHub legt die Ordner dann automatisch an.

### 3. API-Keys als Secrets hinterlegen
- Im Repo: **Settings → Secrets and variables → Actions → New repository secret**.
- Secret 1: Name `FMP_API_KEY`, Wert = dein FMP-Key. **Add secret**.
- *(Optional)* Secret 2: Name `ANTHROPIC_API_KEY`, Wert = dein Anthropic-Key.

### 4. GitHub Pages aktivieren
- **Settings → Pages**.
- Unter „Build and deployment“: Source = **Deploy from a branch**.
- Branch = **main**, Ordner = **/docs**. **Save**.
- Nach ein paar Minuten zeigt die Seite oben die öffentliche Adresse, z. B.
  `https://DEINNAME.github.io/research-dashboard/`. Das ist dein Dashboard.

### 5. Ersten Daten-Lauf manuell starten
- **Actions** → links „Taeglicher Research-Refresh“ → rechts **Run workflow → Run workflow**.
- Der Lauf dauert 1–3 Minuten. Danach ist `docs/data/snapshot.json` mit echten Daten gefüllt.
- Dashboard-Seite neu laden — die Daten erscheinen.

### 6. Fertig
Ab jetzt läuft der Refresh **jeden Morgen automatisch**. Du musst nichts mehr tun.
Zum manuellen Aktualisieren: entweder im Dashboard „↻ Neu laden“ (lädt den letzten Snapshot)
oder in **Actions** erneut „Run workflow“ (holt frische Daten sofort).

---

## Unternehmen bearbeiten

Im Dashboard unter **⚙ Einstellungen → Auswahl Unternehmen**:
- Titel anlegen, bearbeiten, löschen; Wettbewerber und Endmärkte pflegen.
- Wichtig pro Titel: das **FMP-Ticker-Symbol** (z. B. `MTRO.L`, `AMZN`, `MIN.AX`) — danach holt der Refresh Kurse/News.
- Änderungen wirken sofort lokal im Browser. Damit der **tägliche Refresh** sie nutzt:
  1. **„↓ companies.json herunterladen“** klicken.
  2. Die Datei im Repo unter `docs/data/companies.json` ersetzen
     (im Repo die Datei öffnen → Stift-Symbol → alten Inhalt durch neuen ersetzen → Commit,
     oder per **Upload files** überschreiben).
- Der nächste Lauf berücksichtigt die neue Liste automatisch.

---

## Zeitzone der 07:00-Automatik

GitHub-Cron läuft in **UTC**. Voreingestellt ist `0 5 * * *` = **07:00 Sommerzeit (MESZ)** /
06:00 Winterzeit (MEZ). Wenn du es ganzjährig exakt um 07:00 willst, passe in
`.github/workflows/daily.yml` die Cron-Zeile an (z. B. im Winter `0 6 * * *`).

---

## Ehrliche Grenzen

- **Datenabdeckung:** Der FMP-Free-Plan deckt US-Titel sehr gut ab, internationale Börsen
  (ASX, LSE, Wien, Mailand, Toronto) teils lückenhaft. Bei einzelnen Titeln können Kurse,
  News oder Termine fehlen — das Dashboard zeigt dann „keine Daten“ und bleibt im Übrigen
  voll funktionsfähig. Für lückenlose internationale Abdeckung wäre der FMP-Starter-Plan
  (~19 $/Monat) nötig.
- **News & Earnings-Kalender:** Einige FMP-Endpunkte können je nach Plan eingeschränkt sein.
  Kurse/technische Analyse funktionieren auf dem Free-Plan am zuverlässigsten.
- **IR-Events:** Aktuell aus dem FMP-Earnings-Kalender. Weitere IR-Termine (Hauptversammlung,
  Capital Markets Day) lassen sich später ergänzen, indem man im Skript pro Titel die
  IR-Seite ausliest — das ist seitenindividuell und nicht im Free-Setup enthalten.
- **Technisches Signal** ist eine mechanische Lesart der Indikatoren (SMA/RSI/MACD/Bollinger),
  **keine Anlageempfehlung**.

---

## Dateien im Überblick

| Datei | Zweck |
|-------|-------|
| `docs/index.html` | Das komplette Dashboard (Frontend), wird von GitHub Pages ausgeliefert. |
| `docs/data/companies.json` | Universum (Titel, Symbole, Peers, Endmärkte). Einzige Quelle, von Skript und Frontend gelesen. |
| `docs/data/snapshot.json` | Täglich erzeugter Datenstand (News, Events, Kurse, Indikatoren). |
| `scripts/research.py` | Sammler-Skript (FMP-Abruf, TA-Berechnung, optionale KI-Summary). |
| `.github/workflows/daily.yml` | Zeitsteuerung (cron 07:00) + Commit des Snapshots. |
| `requirements.txt` | Nur Doku — das Skript nutzt ausschließlich die Python-Standardbibliothek. |
