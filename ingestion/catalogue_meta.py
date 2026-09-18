"""Official CAG catalogue metadata, with explicitly labelled derived filters."""
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from ingestion.crawler import official_url


def slug(value):
    return re.sub(r"[^\w]+", "-", value.casefold(), flags=re.UNICODE).strip("-") or "unknown"


def canonical_pdf(base, href):
    url = urlsplit(official_url(base, href))
    if not url.path.lower().endswith(".pdf") or "/download_audit_report/" not in url.path:
        raise ValueError("Not an audit PDF")
    return urlunsplit((url.scheme, url.netloc, url.path.replace("/webroot/uploads/", "/uploads/"), url.query, ""))


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def listing_reports(html, base):
    soup = BeautifulSoup(html, "html.parser")
    locale = "hi" if "/hi/" in base else "en"
    records = []
    for card in soup.select(".AuditReportlisting"):
        detail = card.select_one('a[href*="/audit-report/details/"]')
        if not detail:
            raise ValueError("Report card missing detail link; cannot silently skip")
        url = official_url(base, detail["href"])
        jurisdiction = text(card.select_one(".reportIcon h5")) or "unknown"
        image = card.select_one('.reportIcon img[src*="/states/upload/"]')
        jurisdiction_key = slug(urlsplit(image["src"]).path.rsplit("/", 1)[-1].rsplit(".", 1)[0]) if image else slug(jurisdiction)
        sectors = [text(n) for n in card.select(".sectorDetail > div")[1:] if text(n) not in ("", "-")]
        types = [text(n) for n in card.select(".reportType span") if text(n)]
        pdfs = {}
        for anchor in card.select(".pdfBottomReport a[href]"):
            try:
                pdf = canonical_pdf(base, anchor["href"])
                pdfs[pdf] = {"source_url": pdf, "link_label": text(anchor), "document_kind": "full-report"}
            except ValueError:
                continue
        records.append({"report_id": locale + ":" + urlsplit(url).path.rstrip("/").split("/")[-1],
            "report_url": url, "title": text(detail), "jurisdiction_label": jurisdiction,
            "jurisdiction": jurisdiction_key, "catalogue_language": locale,
            "jurisdiction_method": "official-state-icon" if image else "official-label",
            "published_date": text(card.select_one(".dateFirst .dtn")),
            "official_report_types": types, "official_sectors": sectors, "pdfs": list(pdfs.values())})
    if not records:
        raise ValueError("No audit cards; catalogue completion cannot be inferred from missing markup")
    return records


def enrich_report(report, html):
    soup = BeautifulSoup(html, "html.parser")
    metadata = dict(report)
    metadata["government_type"] = "unknown"
    for label in soup.select(".auditRightLable"):
        if text(label.select_one(".labelTitleBold")).casefold() in ("government type", "सरकार का प्रकार"):
            value = text(label.select_one(".labelItemBold"))
            metadata["government_type"] = {"राज्य": "state", "संघ": "union", "स्थानीय निकाय": "local-bodies"}.get(value, slug(value))
    if metadata["jurisdiction"] == "unknown" and metadata["government_type"] == "union":
        metadata["jurisdiction"] = "union"
    sectors = [text(n) for n in soup.select(".sectorSingleAudit > span")[1:] if text(n) not in ("", "-")]
    metadata["official_sectors"] = sectors or report["official_sectors"]
    pdfs = {p["source_url"]: p for p in report["pdfs"]}
    sections = {}
    for anchor in soup.select(".guidelinesList > li > a[href]"):
        try:
            url = canonical_pdf(report["report_url"], anchor["href"])
        except ValueError:
            continue
        label = text(anchor)
        full = (slug(label) == slug(report["title"]) or "full report" in label.casefold()
                or "complete report" in label.casefold())
        record = {"source_url": url, "link_label": label,
                  "document_kind": "full-report" if full else "report-section"}
        if full or url in pdfs:
            pdfs[url] = record
        else:
            sections[url] = record
    # Some older reports only offer chapters. Index those if no full edition exists.
    metadata["pdfs"] = list((pdfs or sections).values())
    metadata["coverage_strategy"] = "full-reports" if pdfs else "section-fallback"
    metadata["section_links_not_separately_indexed"] = len(sections) if pdfs else 0
    if not metadata["pdfs"]:
        raise ValueError("Report has no supported public audit PDF links; needs review")
    return metadata


def document_metadata(report, pdf):
    title = report["title"].casefold()
    categories = {slug(v) for v in report["official_report_types"]}
    # These are useful coarse filters, NOT claimed as official report-type labels.
    derived = []
    for phrase, category in [("state finance", "state-finances"), ("local bod", "local-bodies"),
                              ("performance", "performance"), ("compliance", "compliance"),
                              ("revenue", "revenue"), ("social", "social-sector"),
                              ("economic", "economic-sector"), ("financial", "financial")]:
        if phrase in title:
            categories.add(category)
            derived.append(category)
    if "राज्य" in title and "वित्त" in title:
        categories.add("state-finances")
        derived.append("state-finances")
    years = re.findall(r"\b(?:19|20)\d{2}\b", report["published_date"])
    periods = re.findall(r"\b(?:19|20)\d{2}[-–](?:\d{4}|\d{2})\b", report["title"])
    label = (pdf["source_url"] + " " + pdf["link_label"]).casefold()
    language = "hi" if "hindi" in label or "हिन्दी" in label or "हिंदी" in label else "en" if "english" in label else report.get("catalogue_language", "unknown")
    return {**{k: v for k, v in report.items() if k != "pdfs"}, **pdf,
        "document_id": hashlib.sha256(pdf["source_url"].encode()).hexdigest(),
        "source": "cag", "categories": sorted(categories) or ["uncategorized"],
        "derived_categories": derived, "category_method": "official-types-plus-title-rules-v1",
        "sectors": [slug(s) for s in report["official_sectors"]] or ["unknown"],
        "published_year": int(years[-1]) if years else None,
        "audit_periods": periods, "language": language, "language_method": "link-label-filename-or-catalogue-locale"}