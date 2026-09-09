#!/usr/bin/env python3
"""
Liest die Stundenpläne der Fachhochschule für Finanzen (afz-kw.brandenburg.de)
für einen bestimmten Jahrgang/Kurs (z. B. "E_2026") aus und erzeugt daraus
eine .ics-Kalenderdatei für eine einzelne Seminargruppe (z. B. SG10).

Aufruf:
    python3 stundenplan_to_ics.py

Konfiguration ganz unten in CONFIG.
"""

import re
import sys
import hashlib
from datetime import datetime, timedelta, date, time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://afz-kw.brandenburg.de/"
MAIN_URL = urljoin(BASE_URL, "main_FHF.html")

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

# Uhrzeiten je Stunde (1-basiert). Stunden 9/10 sind eine Annahme
# (Fortsetzung des 45-Minuten-Rasters) - bitte prüfen, sobald die
# Seite die 9./10. Stunde mal tatsächlich befüllt, und ggf. anpassen.
PERIOD_TIMES = {
    1: (time(7, 45), time(8, 30)),
    2: (time(8, 30), time(9, 15)),
    3: (time(9, 45), time(10, 30)),
    4: (time(10, 30), time(11, 15)),
    5: (time(12, 0), time(12, 45)),
    6: (time(12, 45), time(13, 30)),
    7: (time(14, 0), time(14, 45)),
    8: (time(14, 45), time(15, 30)),
    9: (time(15, 45), time(16, 30)),
    10: (time(16, 30), time(17, 15)),
}

DATE_RANGE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2})\.(\d{2})\.(\d{4})")


def clean(text):
    return " ".join(text.replace("\xa0", " ").split()).strip()


def fetch(url):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def find_week_links(main_html, cohort):
    """Findet alle Wochen-Links für einen Jahrgang (z. B. 'E_2026') auf der Übersichtsseite."""
    soup = BeautifulSoup(main_html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"Unterrichtsplan_{cohort}_" in href:
            links.append(urljoin(BASE_URL, href))
    # Duplikate entfernen, Reihenfolge beibehalten
    seen = set()
    ordered = []
    for l in links:
        if l not in seen:
            seen.add(l)
            ordered.append(l)
    return ordered


def parse_week_page(html, sg_index):
    """
    sg_index: 1-basiert, welche Seminargruppen-Spalte (1..10) ausgelesen werden soll.
    Gibt eine Liste von Terminen zurück: dicts mit start, end, subject, room.
    """
    # lxml statt html.parser: html.parser bricht wegen des fehlerhaften HTML
    # der Quellseite (fehlendes <tr> vor den Tages-Kopfzeilen) die Tabelle
    # faktisch nach dem ersten Tag ab. lxml repariert das wie ein Browser.
    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)

    m = DATE_RANGE_RE.search(page_text)
    if not m:
        raise ValueError("Konnte Wochendatum nicht finden (erwartetes Format DD.MM.YYYY - DD.MM.YYYY).")
    d1, m1, y1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    monday = date(y1, m1, d1)

    tables = soup.find_all("table")
    if len(tables) < 2:
        raise ValueError("Erwartete verschachtelte Tabelle mit dem Stundenplan wurde nicht gefunden.")
    plan_table = tables[1]  # die innere Tabelle mit dem eigentlichen Plan

    # WICHTIG: Auf der Quellseite fehlt bei jeder Tages-Kopfzeile (Dienstag,
    # Mittwoch, ...) das öffnende <tr>-Tag - Browser reparieren das beim
    # Rendern automatisch, HTML-Parser aber je nach Engine unterschiedlich
    # (Zeilen gehen sonst verloren). Deshalb hier bewusst NICHT über <tr>
    # gruppieren, sondern über den <td>-Strom in Dokumentreihenfolge - das
    # ist robust unabhängig von fehlenden/fehlerhaften <tr>-Tags.
    cells = plan_table.find_all("td")

    current_day_offset = None
    # Rohdaten je Tag: {offset: {periode: (fach, raum)}}
    day_periods = {}

    i = 0
    n = len(cells)
    while i < n:
        first_text = clean(cells[i].get_text())

        # Tages-Kopfzeile? (z. B. "Montag", gefolgt von 10 SG-Label-Zellen)
        if first_text in WEEKDAYS:
            current_day_offset = WEEKDAYS.index(first_text)
            day_periods.setdefault(current_day_offset, {})
            i += 11  # 1 Tages-Zelle + 10 SG-Label-Zellen überspringen
            continue

        # Stunden-Zeile? (z. B. "1. Std.", gefolgt von 10x (Fach, Raum))
        std_match = re.match(r"(\d+)\.\s*Std\.?", first_text)
        if std_match and current_day_offset is not None:
            period = int(std_match.group(1))
            sg_start = i + 1 + (sg_index - 1) * 2
            if sg_start + 1 < n:
                subject = clean(cells[sg_start].get_text())
                room = clean(cells[sg_start + 1].get_text())
                if subject and room:
                    day_periods[current_day_offset][period] = (subject, room)
            i += 21  # 1 Stunden-Zelle + 20 Fach/Raum-Zellen überspringen
            continue

        i += 1

    # Aufeinanderfolgende gleiche Stunden zu einem Termin zusammenfassen
    events = []
    for offset, periods in day_periods.items():
        event_date = monday + timedelta(days=offset)
        sorted_periods = sorted(periods.keys())
        i = 0
        while i < len(sorted_periods):
            start_period = sorted_periods[i]
            subject, room = periods[start_period]
            end_period = start_period
            j = i + 1
            while (
                j < len(sorted_periods)
                and sorted_periods[j] == end_period + 1
                and periods[sorted_periods[j]] == (subject, room)
            ):
                end_period = sorted_periods[j]
                j += 1

            start_t = PERIOD_TIMES.get(start_period)
            end_t = PERIOD_TIMES.get(end_period)
            if start_t and end_t:
                events.append({
                    "date": event_date,
                    "start": datetime.combine(event_date, start_t[0]),
                    "end": datetime.combine(event_date, end_t[1]),
                    "subject": subject,
                    "room": room,
                    "period_range": f"{start_period}.-{end_period}. Std." if end_period != start_period else f"{start_period}. Std.",
                })
            i = j

    return events


def build_ics(all_events, calendar_name):
    def fold(line):
        # simple line folding nach RFC5545 (75 Oktette), hier ausreichend einfach gehalten
        out = []
        while len(line.encode("utf-8")) > 75:
            out.append(line[:74])
            line = " " + line[74:]
        out.append(line)
        return "\r\n".join(out)

    def esc(s):
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//afz-kw-stundenplan-sync//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(calendar_name)}",
        "X-WR-TIMEZONE:Europe/Berlin",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Berlin",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0200",
        "TZNAME:CEST",
        "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:+0200",
        "TZOFFSETTO:+0100",
        "TZNAME:CET",
        "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for ev in all_events:
        uid_src = f"{ev['date'].isoformat()}-{ev['start'].strftime('%H%M')}-{ev['subject']}-{ev['room']}"
        uid = hashlib.md5(uid_src.encode("utf-8")).hexdigest() + "@afz-kw-stundenplan"
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uid}")
        lines.append(f"DTSTAMP:{now_stamp}")
        lines.append(f"DTSTART;TZID=Europe/Berlin:{ev['start'].strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"DTEND;TZID=Europe/Berlin:{ev['end'].strftime('%Y%m%dT%H%M%S')}")
        lines.append(fold(f"SUMMARY:{esc(ev['subject'])}"))
        lines.append(fold(f"LOCATION:{esc(ev['room'])}"))
        lines.append(fold(f"DESCRIPTION:{esc(ev['period_range'])}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    # ---------------- CONFIG ----------------
    COHORT = "E_2026"      # entspricht "Unterrichtsplan_E_2026_..." Links auf main_FHF.html
    SG_INDEX = 10           # Seminargruppe 10
    OUTPUT_FILE = "stundenplan.ics"
    CALENDAR_NAME = "FHF Stundenplan SG10"
    # -----------------------------------------

    print(f"Lade Übersichtsseite: {MAIN_URL}")
    main_html = fetch(MAIN_URL)
    week_links = find_week_links(main_html, COHORT)
    print(f"Gefundene Wochen-Seiten für {COHORT}: {len(week_links)}")

    all_events = []
    for url in week_links:
        print(f"  -> lese {url}")
        try:
            html = fetch(url)
            events = parse_week_page(html, SG_INDEX)
            print(f"     {len(events)} Termine gefunden")
            all_events.extend(events)
        except Exception as e:
            print(f"     Fehler beim Verarbeiten von {url}: {e}", file=sys.stderr)

    ics_content = build_ics(all_events, CALENDAR_NAME)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(ics_content)
    print(f"Fertig: {len(all_events)} Termine insgesamt in {OUTPUT_FILE} geschrieben.")


if __name__ == "__main__":
    main()
