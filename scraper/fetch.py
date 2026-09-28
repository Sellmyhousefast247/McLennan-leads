#!/usr/bin/env python3
"""
McLennan County (Texas) Motivated Seller Lead Scraper
=====================================================
McLennan's recorder is Tyler EagleWeb Self-Service (anonymous, no login),
so unlike Comal (ROAM account-walled) the recorder INDEX is scraped
directly for the distress document types. Foreclosure trustee-sale
notices are not indexed as full records there (0 results over 8 weeks),
so those come from the County Clerk's monthly scanned posting PDFs.

Sources
  1. Tyler EagleWeb Self-Service recorder (anonymous, requests-only):
       https://mclennancountytx-web.tylerhost.net/web/
     Flow: GET /web/ (session cookie) -> POST /web/user/disclaimer
     (accept) -> GET /web/search/DOCSEARCH402S1 -> per doc type
     POST /web/searchPost/DOCSEARCH402S1 (recording-date range +
     document-type holder) -> GET /web/searchResults/...?page=N.
     Results are one .ss-search-row per document: H1 "docnum . TYPE",
     Recording Date, Grantor, Grantee(n), Legal Description.
     Document types kept (7-day lookback):
       ABSTRACT OF JUDGMENT -> JUD   LIS PENDENS -> LP
       AFFIDAVIT OF HEIRSHIP -> PRO  FEDERAL/STATE TAX LIEN -> LIEN
       HOSPITAL LIENS -> LIEN        MECHANIC LIEN -> LIEN
       CHILD SUPPORT LIEN -> LIEN    NOTICE OF TRUSTEE SALE -> FC
     The distressed party (property owner) is the GRANTOR for liens /
     judgments / heirship; CAD enrichment confirms and fills address.
  2. County Clerk monthly foreclosure-sale posting PDFs (scanned images):
       https://www.mclennan.gov/Archive.aspx?AMID=41  (list) ->
       Archive.aspx?ADID=<n> per "<MONTH> <D>, <YYYY> SALE DATE" entry.
     OCR'd with pdf2image + pytesseract; best-effort address / mortgagor /
     sale-date per notice. cat FC. Failure never zeroes the run.
  3. Enrichment: McLennan CAD parcels (City of Waco GIS, public ArcGIS):
       https://gis.wacotx.gov/server/rest/services/Parcels/FeatureServer/0
     ~143,200 parcels. file_as_name = "LAST FIRST M" uppercase; situs in
     situs_num/_street_prefx/_street/_street_sufix/_city/_zip (situs_street
     excludes the suffix; situs_display is the clean assembled address);
     mailing in addr_line1..3/addr_city/addr_state/addr_zip; value in
     `market`. No Referer needed.

Run:
    python scraper/fetch.py                # default 7-day lookback
    python scraper/fetch.py --days 14
    python scraper/fetch.py --skip-parcel --skip-fcpdf
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COUNTY = "McLennan"
STATE = "TX"

REC_BASE = "https://mclennancountytx-web.tylerhost.net"
REC_SEARCH_ID = "DOCSEARCH402S1"

FC_LIST_URL = "https://www.mclennan.gov/Archive.aspx?AMID=41"
FC_SITE = "https://www.mclennan.gov"

PARCEL_API_URL = ("https://gis.wacotx.gov/server/rest/services/"
                  "Parcels/FeatureServer/0/query")

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
LIEN_LOOKBACK_DAYS = 30      # liens / heirship trickle in slower than AJs
REQUEST_TIMEOUT = 45
ARCGIS_MAX_LOOKUPS = 1500
OCR_DPI = 200
OCR_MAX_PAGES = 400
FC_MONTHS_AHEAD = 3          # OCR up to this many upcoming sale-date PDFs

# Recorder document types -> (holderInput code, holderValue label, cat, cat_label,
#                             lookback_days).  One EagleWeb search per entry.
REC_DOC_TYPES = [
    ("ABSTRACTJUDG", "ABSTRACT OF JUDGMENT",  "JUD",  "Abstract of Judgment", LOOKBACK_DAYS),
    ("LISPEN",       "LIS PENDENS",           "LP",   "Lis Pendens",          LIEN_LOOKBACK_DAYS),
    ("AFFHEIRSHIP",  "AFFIDAVIT OF HEIRSHIP", "PRO",  "Affidavit of Heirship", LIEN_LOOKBACK_DAYS),
    ("FEDTAXLN",     "FEDERAL TAX LIEN",      "LIEN", "Federal Tax Lien",     LIEN_LOOKBACK_DAYS),
    ("STATETXLN",    "STATE TAX LIEN",        "LIEN", "State Tax Lien",       LIEN_LOOKBACK_DAYS),
    ("HOSPLIEN",     "HOSPITAL LIENS",        "LIEN", "Hospital Lien",        LIEN_LOOKBACK_DAYS),
    ("ML",           "MECHANIC LIEN",         "LIEN", "Mechanic Lien",        LOOKBACK_DAYS),
    ("CHILDSUPTLN",  "CHILD SUPPORT LIEN",    "LIEN", "Child Support Lien",   LIEN_LOOKBACK_DAYS),
    ("NOTICETRSALE", "NOTICE OF TRUSTEE SALE", "FC",  "Notice of Trustee Sale", LIEN_LOOKBACK_DAYS),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mclennan_scraper")

GHL_FIELDS = [
    "doc_num","doc_type","cat","cat_label","filed","owner","grantee",
    "amount","prop_address","prop_city","prop_state","prop_zip",
    "mail_address","mail_city","mail_state","mail_zip","legal","clerk_url","score","flags",
    "first_seen","status",
]
GHL_HEADERS = {f: f.replace("_", " ").title() for f in GHL_FIELDS}
GHL_HEADERS["first_seen"] = "Date Entered System"
GHL_HEADERS["status"] = "Status"

@dataclass
class LeadRecord:
    doc_num: str = ""
    doc_type: str = ""
    cat: str = ""
    cat_label: str = ""
    filed: str = ""
    owner: str = ""
    grantee: str = ""
    amount: float = 0.0
    legal: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = STATE
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = STATE
    mail_zip: str = ""
    clerk_url: str = ""
    flags: list = field(default_factory=list)
    score: int = 0
    status: str = ""
    first_seen: str = ""
    rid: str = ""
    content_hash: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_date(raw: str) -> str:
    raw = _norm_ws(raw)
    # EagleWeb dates are "MM/DD/YYYY HH:MM AM"; keep the date part
    raw = raw.split(" ")[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _arc_val(x) -> str:
    s = _norm_ws(x)
    return "" if s.upper() in ("NULL", "NONE") else s


def _sql_lit(s: str) -> str:
    return s.upper().replace("'", "''")


ENTITY_RE = re.compile(
    r"\b(LLC|L\.?L\.?C|INC|CORP|COMPANY|CO|BANK|N\.?A|TRUST|LP|L\.?P|LLP|"
    r"ASSOCIATION|ASSN|FUND|FUNDING|CREDIT UNION|CU|SYSTEM|COUNTY|CITY OF|"
    r"STATE OF|UNITED STATES|IRS|DEPARTMENT|ISD|UNIVERSITY|COLLEGE|"
    r"HOSPITAL|MEDICAL|SERVICES|CAPITAL|MORTGAGE|FINANCIAL|HOLDINGS)\b", re.I)


def _looks_like_entity(name: str) -> bool:
    return bool(ENTITY_RE.search(name or ""))


def _pick_owner(grantor: str, grantees: list) -> tuple:
    """Choose (owner, counterparty) from a recorder row. The grantee is the
    party the lien/judgment runs against (the owner); the grantor is the
    institutional filer. Prefer whichever side is a person so that
    occasionally-reversed indexing still yields the individual."""
    gtee = (grantees[0] if grantees else "").strip()
    gtor = (grantor or "").strip()
    gtee_ent, gtor_ent = _looks_like_entity(gtee), _looks_like_entity(gtor)
    if gtee and not gtee_ent:
        return gtee, gtor
    if gtor and not gtor_ent:
        return gtor, gtee
    # both entities (or one side blank): keep the grantee as owner
    return (gtee or gtor), (gtor if gtee else "")


def normalize_owner_for_parcel(name: str) -> str:
    """CAD file_as_name is 'LAST FIRST M' uppercase.

    Recorder grantor names come as 'LAST FIRST M' (person, most common) or
    'FIRST LAST'.  Produce a 'LAST FIRST' two-token prefix for LIKE
    matching.  Entities are returned uppercased as-is.
    """
    if not name:
        return ""
    n = _norm_ws(name).upper()
    n = re.sub(r"\b(JR|SR|II|III|IV)\.?$", "", n).strip().rstrip(",")
    n = re.sub(r"\b(AKA|A/K/A|DBA|D/B/A|FKA|F/K/A)\b.*$", "", n).strip().rstrip(",")
    if _looks_like_entity(n):
        return n
    if "," in n:
        last, first = n.split(",", 1)
        first_tok = first.strip().split()
        return f"{last.strip()} {first_tok[0] if first_tok else ''}".strip()
    parts = n.split()
    if len(parts) >= 2:
        # Recorder person names are already "LAST FIRST M" -> keep first two.
        return f"{parts[0]} {parts[1]}"
    return n

# ---------------------------------------------------------------------------
# Tyler EagleWeb Self-Service recorder scraper (Playwright)
# ---------------------------------------------------------------------------
class EagleWebRecorder:
    """Anonymous Tyler EagleWeb Self-Service. The searchPost endpoint is
    session/token bound to a freshly-picked autocomplete document type, so
    a raw requests replay is rejected; the search form is driven with
    Playwright instead. Per doc type: load the search page, fill the
    recording-date range, type + pick the document type from the
    autocomplete, click Search, parse the single results page (EagleWeb
    returns all rows on one page; paginate defensively if not)."""

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0.0.0 Safari/537.36")

    SEARCH_URL = f"{REC_BASE}/web/search/{REC_SEARCH_ID}"

    def __init__(self, end: datetime):
        self.end = end

    def _accept_disclaimer(self, page) -> None:
        page.goto(f"{REC_BASE}/web/user/disclaimer",
                  wait_until="domcontentloaded", timeout=45000)
        try:
            page.click("#submitDisclaimerAccept", timeout=8000)
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            # Some sessions land straight on the home menu.
            page.goto(f"{REC_BASE}/web/", wait_until="domcontentloaded",
                      timeout=30000)

    def _run_one(self, page, code: str, label: str,
                 start: datetime, end: datetime) -> str:
        page.goto(self.SEARCH_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_selector("input[name=field_RecDateID_DOT_StartDate]",
                               timeout=30000)
        page.fill("input[name=field_RecDateID_DOT_StartDate]",
                  start.strftime("%m/%d/%Y"))
        page.fill("input[name=field_RecDateID_DOT_EndDate]",
                  end.strftime("%m/%d/%Y"))
        # document-type autocomplete: type then pick the exact option
        dt = page.locator("input[name=field_selfservice_documentTypes]")
        dt.click()
        dt.fill("")
        dt.type(label, delay=25)
        page.wait_for_timeout(1500)
        picked = False
        for sel in (f"li:has-text('{label}')", "ul.ui-autocomplete li",
                    "li[role=option]"):
            try:
                opt = page.locator(sel).filter(has_text=label).first
                if opt.count():
                    opt.click(timeout=4000)
                    picked = True
                    break
            except Exception:
                continue
        if not picked:
            # fall back to keyboard select of first suggestion
            dt.press("ArrowDown")
            dt.press("Enter")
        page.wait_for_timeout(400)
        page.click("#searchButton")
        # wait for results rows or the no-results banner
        try:
            page.wait_for_selector(
                ".ss-search-row, text=No results found", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(600)
        return page.content()

    @staticmethod
    def _total_pages(html: str) -> int:
        m = re.search(r"page\s+\d+\s+of\s+(\d+)", html, re.I)
        return int(m.group(1)) if m else 1

    @staticmethod
    def _parse_rows(html: str):
        soup = BeautifulSoup(html, "lxml")
        for row in soup.select(".ss-search-row"):
            h1 = row.find("h1")
            if not h1:
                continue
            head = _norm_ws(h1.get_text(" "))
            # "2026030952 • ABSTRACT OF JUDGMENT" -> doc_num is the leading token
            m = re.match(r"([0-9A-Za-z][0-9A-Za-z\-]*)\s*[•·.\-]?\s*(.*)$",
                         head)
            doc_num = m.group(1) if m else head.split()[0]
            filed = grantor = ""
            grantees, legals = [], []
            for col in row.select(".searchResultFourColumn"):
                items = col.select("li")
                if not items:
                    continue
                header = _norm_ws(items[0].get_text(" ")).lower()
                vals = [_norm_ws(li.get_text(" ")) for li in items[1:]
                        if _norm_ws(li.get_text(" "))]
                if header.startswith("recording date"):
                    filed = vals[0] if vals else ""
                elif header.startswith("grantor"):
                    grantor = vals[0] if vals else ""
                elif header.startswith("grantee"):
                    grantees = vals
                elif header.startswith("legal"):
                    legals = vals
            yield {
                "doc_num": doc_num, "filed": filed, "grantor": grantor,
                "grantees": grantees, "legal": " / ".join(legals[:2]),
            }

    def run(self) -> list:
        records = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=self._UA)
            page = ctx.new_page()
            try:
                self._accept_disclaimer(page)
            except Exception as exc:
                log.warning("EagleWeb disclaimer step failed: %s", exc)

            for code, label, cat, cat_label, days in REC_DOC_TYPES:
                start = self.end - timedelta(days=days)
                try:
                    html = self._run_one(page, code, label, start, self.end)
                    pages = self._total_pages(html)
                    rows = list(self._parse_rows(html))
                    for p in range(2, min(pages, 40) + 1):
                        try:
                            page.goto(
                                f"{REC_BASE}/web/searchResults/{REC_SEARCH_ID}"
                                f"?page={p}",
                                wait_until="domcontentloaded", timeout=30000)
                            rows.extend(self._parse_rows(page.content()))
                        except Exception:
                            break
                    n = 0
                    for row in rows:
                        if not row["doc_num"] or not re.search(r"\d", row["doc_num"]):
                            continue
                        # The distressed party (property owner) is the party the
                        # instrument runs against -- the GRANTEE for abstracts of
                        # judgment, tax/hospital/mechanic/child-support liens, lis
                        # pendens and trustee notices; the GRANTOR is the creditor /
                        # plaintiff / taxing authority. Prefer the non-entity party
                        # so reversed-indexed filings (e.g. some federal tax liens)
                        # still resolve to the individual.
                        owner, counter = _pick_owner(
                            row["grantor"], row["grantees"])
                        records.append(LeadRecord(
                            doc_num=row["doc_num"], doc_type=label,
                            cat=cat, cat_label=cat_label,
                            filed=normalize_date(row["filed"]),
                            owner=owner, grantee=counter,
                            legal=row["legal"],
                            clerk_url=self.SEARCH_URL,
                        ))
                        n += 1
                    log.info("EagleWeb %-22s: %d records (%d page[s])",
                             label, n, pages)
                except Exception as exc:
                    log.warning("EagleWeb search %s failed: %s", label, exc)
            browser.close()
        return records

# ---------------------------------------------------------------------------
# Monthly foreclosure posting PDFs (scanned -> OCR)
# ---------------------------------------------------------------------------
STREET_RE = re.compile(
    r"\b(\d{2,6}\s+[A-Z][A-Za-z0-9 .'-]{2,40}?"
    r"(?:ROAD|RD|STREET|ST|DRIVE|DR|LANE|LN|COURT|CT|CIRCLE|CIR|TRAIL|TRL|"
    r"AVENUE|AVE|BOULEVARD|BLVD|WAY|PASS|PATH|LOOP|RUN|COVE|CV|BEND|BND|PARK|"
    r"PKWY|PARKWAY|HOLLOW|HL|HILL|HILLS|RIDGE|CANYON|CREEK|VALLEY|VIEW|"
    r"CROSSING|XING|TERRACE|TER|PLACE|PL|POINT|PT|SPRINGS?|MEADOWS?|OAKS?))"
    r"\b[,.]?\s*(?:#?\s*\d+[A-Z]?)?", re.I)
ZIP_RE = re.compile(r"\b(WACO|BELLMEAD|HEWITT|WOODWAY|LORENA|ROBINSON|MCGREGOR|"
                    r"CRAWFORD|MART|WEST|MOODY|RIESEL|BRUCEVILLE|EDDY|ELM MOTT|"
                    r"CHINA SPRING|GHOLSON|LEROY|LACY LAKEVIEW|BEVERLY HILLS|"
                    r"GOLINDA|ROSEBUD|AXTELL|VALLEY MILLS)\b[^0-9]{0,20}(\d{5})", re.I)
SALE_DATE_RE = re.compile(r"(January|February|March|April|May|June|July|August|"
                          r"September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})", re.I)
ARCHIVE_RE = re.compile(
    r'href="(Archive\.aspx\?ADID=(\d+))"[^>]*>\s*([A-Z]+\s+\d+,\s+20\d{2})\s+SALE DATE',
    re.I)


def fetch_fc_pdf_records(session) -> list:
    """Download upcoming monthly trustee-sale posting PDFs and OCR them.
    Each OCR page that looks like a notice becomes one cat=FC record with
    best-effort address / mortgagor / sale-date. Never raises."""
    records = []
    try:
        r = session.get(FC_LIST_URL, timeout=REQUEST_TIMEOUT)
        entries = ARCHIVE_RE.findall(r.text)
        if not entries:
            log.warning("FC posting list: no sale-date PDFs found")
            return records
        # entries: (href, adid, "MONTH D, YYYY"). Keep upcoming sale dates,
        # newest first, capped.
        parsed = []
        for href, adid, saletxt in entries:
            try:
                sd = datetime.strptime(_norm_ws(saletxt), "%B %d, %Y")
            except ValueError:
                sd = None
            parsed.append((sd, href, adid, saletxt))
        upcoming = [p for p in parsed if p[0] and p[0] >= datetime.now() - timedelta(days=5)]
        chosen = (sorted(upcoming, key=lambda p: p[0])[:FC_MONTHS_AHEAD]
                  or sorted([p for p in parsed if p[0]], key=lambda p: p[0], reverse=True)[:1])
        log.info("FC posting PDFs chosen: %s",
                 ", ".join(_norm_ws(c[3]) for c in chosen) or "none")

        from pdf2image import convert_from_bytes
        import pytesseract

        for sd, href, adid, saletxt in chosen:
            pdf_url = f"{FC_SITE}/{href}"
            try:
                pdf_bytes = session.get(pdf_url, timeout=180).content
            except Exception as exc:
                log.warning("FC PDF %s download failed: %s", adid, exc)
                continue
            if not pdf_bytes[:4] == b"%PDF":
                log.warning("FC ADID=%s not a PDF (%d bytes)", adid, len(pdf_bytes))
                continue
            log.info("FC PDF ADID=%s (%s): %d bytes", adid, _norm_ws(saletxt), len(pdf_bytes))
            try:
                pages = convert_from_bytes(pdf_bytes, dpi=OCR_DPI)[:OCR_MAX_PAGES]
            except Exception as exc:
                log.warning("FC PDF ADID=%s render failed: %s", adid, exc)
                continue
            notices = []
            for idx, img in enumerate(pages, start=1):
                try:
                    text = pytesseract.image_to_string(img)
                except Exception:
                    continue
                if re.search(r"NOTICE\s+OF.{0,40}SALE", text, re.I) or not notices:
                    notices.append({"page": idx, "text": text})
                else:
                    notices[-1]["text"] += "\n" + text

            saledate = sd.strftime("%Y-%m-%d") if sd else ""
            got = 0
            for n in notices:
                text = n["text"]
                addr_m = STREET_RE.search(text.upper())
                zip_m = ZIP_RE.search(text)
                mort_m = re.search(
                    r"(?:executed by|Grantor\(?s?\)?:?|Mortgagor\(?s?\)?:?)\s+"
                    r"([A-Z][A-Za-z ,.'-]{4,60})", text)
                rec = LeadRecord(
                    doc_num=f"FCPDF-{adid}-p{n['page']}",
                    doc_type="NOTICE OF TRUSTEE SALE",
                    cat="FC",
                    cat_label=(f"Trustee Sale {saledate}" if saledate
                               else "Trustee Sale Posting"),
                    filed=datetime.now().strftime("%Y-%m-%d"),
                    owner=_norm_ws(mort_m.group(1)) if mort_m else "",
                    prop_address=_norm_ws(addr_m.group(1)).title() if addr_m else "",
                    prop_city=_norm_ws(zip_m.group(1)).title() if zip_m else "",
                    prop_zip=zip_m.group(2) if zip_m else "",
                    legal=(f"Trustee sale date: {saledate}. " if saledate else "") +
                          f"OCR of posted notice ADID {adid}, page {n['page']}",
                    clerk_url=pdf_url,
                )
                records.append(rec)
                got += 1
            log.info("FC ADID=%s: %d notices (%d with address)",
                     adid, got, sum(1 for x in records
                                    if x.doc_num.startswith(f"FCPDF-{adid}-") and x.prop_address))
    except Exception as exc:
        log.warning("FC PDF source failed (skipping): %s", exc)
    return records

# ---------------------------------------------------------------------------
# McLennan CAD parcel enrichment (City of Waco public ArcGIS)
# ---------------------------------------------------------------------------
PARCEL_FIELDS = ("file_as_name,situs_num,situs_street_prefx,situs_street,"
                 "situs_street_sufix,situs_city,situs_zip,situs_display,"
                 "addr_line1,addr_line2,addr_line3,addr_city,addr_state,"
                 "addr_zip,market,PROP_ID")


def _situs_from(att: dict) -> tuple:
    # Assemble the street from the structured situs fields (unambiguous:
    # situs_street excludes prefix/suffix). City/zip come from their own
    # fields. situs_display is a last-resort fallback.
    city = _arc_val(att.get("situs_city")).title()
    zp = _arc_val(att.get("situs_zip"))[:5]
    street = " ".join(x for x in (
        _arc_val(att.get("situs_num")), _arc_val(att.get("situs_street_prefx")),
        _arc_val(att.get("situs_street")), _arc_val(att.get("situs_street_sufix"))) if x)
    street = _norm_ws(street)
    if street:
        return street.title(), city, zp
    disp = _arc_val(att.get("situs_display"))
    if disp:
        m = re.match(r"(.*?)\s+([A-Z][A-Z .]+?),\s+(\d{5})\s*$", disp)
        if m:
            return _norm_ws(m.group(1)).title(), m.group(2).title(), m.group(3)
        return _norm_ws(disp).title(), city, zp
    return "", city, zp


def _mailing_from(att: dict) -> tuple:
    street = (_arc_val(att.get("addr_line1")) or _arc_val(att.get("addr_line2"))
              or _arc_val(att.get("addr_line3")))
    return (street, _arc_val(att.get("addr_city")).title(),
            _arc_val(att.get("addr_state")) or STATE,
            _arc_val(att.get("addr_zip"))[:5])


def _arcgis_query(session, where: str, count: int = 5) -> list:
    params = {
        "where": where, "outFields": PARCEL_FIELDS,
        "returnGeometry": "false", "f": "json", "resultRecordCount": count,
    }
    try:
        r = session.get(PARCEL_API_URL, params=params, timeout=REQUEST_TIMEOUT)
        return r.json().get("features", []) or []
    except Exception as exc:
        log.debug("ArcGIS query error: %s", exc)
        return []


def _addr_key(addr: str) -> tuple:
    m = re.match(r"\s*(\d+)\s+(.*)", addr or "")
    if not m:
        return "", ""
    rest = _norm_ws(m.group(2))
    rest = re.sub(r"\s+(#|APT|UNIT|STE|SUITE|BLDG|LOT)\b.*$", "", rest, flags=re.I)
    # situs_street excludes the suffix; drop a trailing suffix word
    rest = re.sub(r"\s+(RD|ROAD|ST|STREET|DR|DRIVE|LN|LANE|CT|COURT|CIR|CIRCLE|"
                  r"TRL|TRAIL|AVE|AVENUE|BLVD|WAY|PASS|PATH|LOOP|RUN|CV|COVE|"
                  r"BND|BEND|PKWY|PARKWAY|TER|TERRACE|PL|PLACE|PT|POINT|XING|"
                  r"CROSSING)\.?$", "", rest, flags=re.I).strip()
    # strip a leading directional prefix so it aligns with situs_street
    rest = re.sub(r"^(N|S|E|W|NE|NW|SE|SW)\s+", "", rest, flags=re.I).strip()
    return m.group(1), rest


def enrich_parcels(records: list) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "McLennanLeadScraper/1.0"

    fwd = [r for r in records if r.owner and (not r.prop_address or not r.mail_address)]
    log.info("CAD owner-lookup for %d records...", len(fwd))
    hits = 0
    for rec in fwd[:ARCGIS_MAX_LOOKUPS]:
        norm = normalize_owner_for_parcel(rec.owner)
        if not norm or len(norm) < 5:
            continue
        feats = _arcgis_query(session, f"UPPER(file_as_name) LIKE '{_sql_lit(norm)}%'")
        if not feats:
            continue
        att = feats[0].get("attributes", {})
        if not rec.prop_address and len(feats) == 1:
            ps, pc, pz = _situs_from(att)
            if ps:
                rec.prop_address, rec.prop_city, rec.prop_zip = ps, pc or rec.prop_city, pz
        if not rec.mail_address:
            ms, mc, mst, mz = _mailing_from(att)
            if ms:
                rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
                hits += 1
        if not rec.amount:
            try:
                rec.amount = float(att.get("market") or 0)
            except (TypeError, ValueError):
                pass
        time.sleep(0.1)
    log.info("CAD owner-lookup: %d mailing fills", hits)

    rev = [r for r in records if r.prop_address and not r.owner]
    log.info("CAD address-lookup for %d records...", len(rev))
    hits = 0
    for rec in rev[:ARCGIS_MAX_LOOKUPS]:
        num, core = _addr_key(rec.prop_address)
        if not num or not core:
            continue
        feats = _arcgis_query(
            session, f"situs_num = '{_sql_lit(num)}' AND "
                     f"UPPER(situs_street) LIKE '{_sql_lit(core)}%'")
        if len(feats) == 1:
            att = feats[0].get("attributes", {})
            owner = _arc_val(att.get("file_as_name"))
            if owner:
                rec.owner = owner
                hits += 1
            if not rec.mail_address:
                ms, mc, mst, mz = _mailing_from(att)
                if ms:
                    rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
        time.sleep(0.1)
    log.info("CAD address-lookup: %d owner fills", hits)

# ---------------------------------------------------------------------------
# Hash / dedupe + NEW-CHANGED detection
# ---------------------------------------------------------------------------
def _repo_base() -> Path:
    return Path(__file__).parent.parent


def _record_rid(r) -> str:
    basis = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}|{r.prop_address}"
    return hashlib.sha1(f"mclennan|{basis}".encode()).hexdigest()[:16]


def _record_chash(r) -> str:
    fields = "|".join(str(x or "") for x in (
        r.doc_num, r.doc_type, r.filed, r.owner, r.grantee, r.legal,
        r.amount, r.prop_address, r.mail_address))
    return hashlib.sha1(fields.encode()).hexdigest()[:16]


def detect_changes(records: list) -> None:
    state_path = _repo_base() / "data" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    n_new = n_chg = n_exist = 0
    for r in records:
        r.rid = _record_rid(r)
        r.content_hash = _record_chash(r)
        prev = state.get(r.rid)
        if prev is None:
            r.status, r.first_seen = "NEW", today
            n_new += 1
        elif prev.get("content_hash") != r.content_hash:
            r.status = "CHANGED"
            r.first_seen = prev.get("first_seen", today)
            n_chg += 1
        else:
            r.status = "EXISTING"
            r.first_seen = prev.get("first_seen", today)
            n_exist += 1
        state[r.rid] = {"content_hash": r.content_hash,
                        "first_seen": r.first_seen, "last_seen": today}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("NEW/CHANGED: NEW=%d CHANGED=%d EXISTING=%d (state=%d ids)",
             n_new, n_chg, n_exist, len(state))

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_records(records: list, start: datetime) -> None:
    for r in records:
        s, flags = 30, []
        if r.cat == "LP": s += 10; flags.append("LIS_PENDENS")
        if r.cat == "FC": s += 15; flags.append("FORECLOSURE")
        if r.cat == "TAXFC": s += 18; flags.append("TAX_FORECLOSURE")
        if r.cat == "TAXDEED": s += 10; flags.append("TAX_DEED")
        if r.cat in ("LP","FC","TAXFC"): s += 5
        if r.cat == "JUD": s += 8; flags.append("JUDGMENT")
        if r.cat == "LIEN": s += 7; flags.append("LIEN")
        if r.cat == "PRO": s += 12; flags.append("PROBATE")
        if r.amount > 100000: s += 15; flags.append("HIGH_AMOUNT")
        elif r.amount > 50000: s += 10; flags.append("MID_AMOUNT")
        if r.filed:
            try:
                if datetime.strptime(r.filed, "%Y-%m-%d") >= start:
                    s += 5; flags.append("NEW_THIS_WEEK")
            except ValueError:
                pass
        if r.prop_address:
            s += 5; flags.append("HAS_ADDRESS")
        r.score = min(s, 100)
        r.flags = flags

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DASH_CAT = {
    "LP": "foreclosure", "FC": "foreclosure", "TAXFC": "foreclosure",
    "TAXDEED": "tax_lien", "LIEN": "tax_lien",
    "JUD": "judgment", "PRO": "probate",
}
FLAG_NICE = {
    "LIS_PENDENS": "Lis pendens", "FORECLOSURE": "Pre-foreclosure",
    "TAX_FORECLOSURE": "Tax foreclosure", "TAX_DEED": "Tax deed",
    "JUDGMENT": "Judgment lien", "LIEN": "Tax lien",
    "PROBATE": "Probate / estate", "HIGH_AMOUNT": "Amount > $100k",
    "MID_AMOUNT": "Amount > $50k", "NEW_THIS_WEEK": "New this week",
    "HAS_ADDRESS": "Has address",
}


def write_outputs(records: list, start: datetime, end: datetime) -> None:
    base = _repo_base()
    for d in [base / "dashboard", base / "data"]:
        d.mkdir(parents=True, exist_ok=True)
    week_ago = (end - timedelta(days=7)).strftime("%Y-%m-%d")
    recs_out = []
    for r in records:
        d = asdict(r)
        d["cat_code"] = r.cat
        d["cat"] = DASH_CAT.get(r.cat, "tax_lien")
        d["flags"] = [FLAG_NICE.get(f, f) for f in (r.flags or [])]
        d["absentee"] = bool(
            r.prop_address and r.mail_address
            and r.prop_address.upper() != r.mail_address.upper())
        d["out_of_state"] = bool(r.mail_state and r.mail_state.upper() != STATE)
        recs_out.append(d)
    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "county": COUNTY,
        "source": f"{COUNTY} County, {STATE} -- EagleWeb Recorder + FC Postings + McLennan CAD",
        "date_range": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
        "total": len(records),
        "new_7d": sum(1 for r in records if (r.first_seen or "") >= week_ago),
        "with_address": sum(1 for r in records if r.prop_address),
        "by_cat": {c: sum(1 for r in records if r.cat == c) for c in ("FC","TAXFC","TAXDEED","LP","JUD","LIEN","PRO")},
        "records": recs_out,
    }
    for path in [base / "dashboard" / "records.json", base / "data" / "records.json"]:
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info("JSON written: %s (%d records)", path, len(records))
    csv_path = base / "data" / "ghl_export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(GHL_HEADERS.values()))
        writer.writeheader()
        for r in records:
            d = asdict(r)
            writer.writerow({GHL_HEADERS[k]: ("|".join(d[k]) if k=="flags" else d[k]) for k in GHL_FIELDS})
    log.info("GHL CSV written: %s (%d records)", csv_path, len(records))
    skip_path = base / "data" / "skiptrace_export.csv"
    skip_cols = ["First Name", "Last Name", "Mailing Address", "Mailing City",
                 "Mailing State", "Mailing Zip", "Property Address",
                 "Property City", "Property State", "Property Zip"]
    with open(skip_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=skip_cols)
        writer.writeheader()
        for r in records:
            owner = (r.owner or "").strip()
            if "," in owner:
                p = owner.split(",", 1)
                first, last = p[1].strip().title(), p[0].strip().title()
            elif _looks_like_entity(owner):
                first, last = "", owner.title()
            else:
                p = owner.split()
                if p and owner == owner.upper() and len(p) > 1:
                    # recorder/CAD "LAST FIRST M" style
                    first, last = p[1].title(), p[0].title()
                else:
                    # "First Last" style
                    first = p[0].title() if p else ""
                    last = p[-1].title() if len(p) > 1 else ""
            writer.writerow({
                "First Name": first, "Last Name": last,
                "Mailing Address": r.mail_address, "Mailing City": r.mail_city,
                "Mailing State": r.mail_state, "Mailing Zip": r.mail_zip,
                "Property Address": r.prop_address, "Property City": r.prop_city,
                "Property State": r.prop_state, "Property Zip": r.prop_zip,
            })
    log.info("Skip trace CSV written: %s (%d records)", skip_path, len(records))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="McLennan County lead scraper")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--skip-parcel", action="store_true")
    parser.add_argument("--skip-fcpdf", action="store_true")
    args = parser.parse_args()
    end = datetime.now()
    start = end - timedelta(days=max(args.days, LIEN_LOOKBACK_DAYS))
    log.info("=" * 60)
    log.info("McLennan County Motivated Seller Lead Scraper")
    log.info("Lookback: judgments/mech %dd, liens/heirship/LP/FC %dd",
             args.days, LIEN_LOOKBACK_DAYS)
    log.info("=" * 60)

    recorder = EagleWebRecorder(end)
    records = recorder.run()

    if not args.skip_fcpdf:
        session = requests.Session()
        session.headers["User-Agent"] = EagleWebRecorder._UA
        fc = fetch_fc_pdf_records(session)
        seen_docs = {r.doc_num for r in records}
        records.extend(r for r in fc if r.doc_num not in seen_docs)

    # dedupe on doc_num
    seen, unique = set(), []
    for r in records:
        key = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    records = unique

    if not args.skip_parcel:
        enrich_parcels(records)
    detect_changes(records)
    score_records(records, start)
    records.sort(key=lambda r: (r.status != "NEW", -r.score))
    if not records:
        log.warning("No records found. Writing empty output files.")
    else:
        log.info("Total after dedup + enrichment: %d", len(records))
    write_outputs(records, start, end)
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Total records  : %d", len(records))
    log.info("  With address   : %d", sum(1 for r in records if r.prop_address))
    log.info("  Score >= 70    : %d", sum(1 for r in records if r.score >= 70))
    log.info("  Score >= 50    : %d", sum(1 for r in records if r.score >= 50))
    for c in ("FC","LP","JUD","LIEN","PRO"):
        log.info("  cat %-5s      : %d", c, sum(1 for r in records if r.cat == c))


if __name__ == "__main__":
    main()
