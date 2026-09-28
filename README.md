# McLennan-leads

Motivated-seller lead scraper for **McLennan County, TX** (Waco). Cloned
from the Bexar/Dallas/Comal county scraper systems.

**Live dashboard:** https://sellmyhousefast247.github.io/McLennan-leads/

## Sources
- **Tyler EagleWeb Self-Service recorder** (anonymous, no login):
  https://mclennancountytx-web.tylerhost.net/web/
  - Distress document types pulled by recording-date range: Abstract of
    Judgment → judgment, Lis Pendens → foreclosure-adjacent, Federal/State
    Tax Lien + Hospital + Mechanic + Child Support Lien → liens, Affidavit
    of Heirship → probate/estate, Notice of Trustee Sale → foreclosure.
  - The distressed party (property owner) is the **grantee** on these
    instruments (the grantor is the creditor / plaintiff / taxing
    authority); a non-entity preference keeps the individual even on
    reversed-indexed filings.
  - Driven with Playwright: the `searchPost` endpoint is session/token
    bound to a freshly-picked autocomplete document type, so a raw
    request replay is rejected. A `#submitDisclaimerAccept` click
    establishes the session; one search per document type follows.
- **County Clerk monthly foreclosure-sale posting PDFs** (scanned images)
  from https://www.mclennan.gov/Archive.aspx?AMID=41 — one PDF per
  "<Month> <D>, <Year> SALE DATE" entry, OCR'd in CI with tesseract;
  best-effort address / mortgagor / sale-date per notice → cat FC.
- **McLennan CAD parcels** (City of Waco public ArcGIS FeatureServer,
  ~143,200 parcels) — owner-forward and address-reverse enrichment
  (situs + mailing + market value). `situs_display` is the clean
  assembled property address; `file_as_name` is `LAST FIRST M`.

## Pipeline
County → scrape → normalize → hash/dedupe → NEW/CHANGED detection →
score → export (`dashboard/records.json`, `data/ghl_export.csv`,
`data/skiptrace_export.csv`). State in `data/state.json`.

## Runs
Daily via GitHub Actions (13:00 UTC) + manual `workflow_dispatch`.

## McLennan-specific notes
- Unlike Comal (recorder account-walled), McLennan's recorder index is
  scraped directly, so liens / judgments / heirship / lis pendens come as
  full records with grantor+grantee. Only trustee-sale **notices** are
  thin in the index (0 over 8 weeks tested), so those come from the
  Clerk's monthly scanned posting PDFs instead.
- EagleWeb returns all results for a search on a single page (no
  pagination observed); the scraper still paginates defensively.
- Foreclosure posting PDFs are scanned images; OCR quality varies, so FC
  records may carry partial addresses. Address-reverse CAD lookup fills
  owners where the OCR address is clean.
- The CAD FeatureServer is the City of Waco public ArcGIS server and
  needs no Referer header (Comal's BIS proxy did).
