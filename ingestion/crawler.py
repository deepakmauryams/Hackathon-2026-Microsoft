"""Small, sequential CAG report crawler. No login, CAPTCHA or access-control bypass."""

import hashlib
import logging
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import httpx

from ingestion.io import write_jsonl

START_URL = "https://cag.gov.in/en/audit-report"
USER_AGENT = "CagResearchIngest/0.1"
LOG = logging.getLogger(__name__)


def official_url(base: str, href: str) -> str:
    url = urlsplit(urljoin(base, href))
    if (url.scheme != "https" or url.hostname != "cag.gov.in"
            or url.port not in (None, 443) or url.username or url.password):
        raise ValueError("Only HTTPS URLs on cag.gov.in are allowed")
    return urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))


def report_links(html: str, base: str) -> list[dict]:
    """Only full-report links inside report cards, not navigation/press releases."""
    soup = BeautifulSoup(html, "html.parser")
    reports = []
    seen = set()
    for card in soup.select(".AuditReportlisting"):
        detail = card.select_one('a[href*="/audit-report/details/"]')
        for anchor in card.select(".pdfBottomReport a[href]"):
            try:
                url = official_url(base, anchor["href"])
            except ValueError:
                continue
            if (not urlsplit(url).path.lower().endswith(".pdf")
                    or "download_audit_report/" not in url or url in seen):
                continue
            seen.add(url)
            title = (detail.get_text(" ", strip=True) if detail
                     else anchor.get("title") or anchor.get_text(" ", strip=True))
            date = card.select_one(".dateFirst .dtn")
            state = card.select_one(".reportIcon h5")
            reports.append({
                "title": title, "source_url": url,
                "report_url": official_url(base, detail["href"]) if detail else base,
                "published_date": date.get_text(" ", strip=True) if date else None,
                "jurisdiction": state.get_text(" ", strip=True) if state else None,
            })
            break  # At most one full PDF per report card.
    return reports


def next_listing(html: str, base: str) -> str | None:
    current = BeautifulSoup(html, "html.parser")
    from urllib.parse import parse_qs
    number = int(parse_qs(urlsplit(base).query).get("page", ["1"])[0])
    for anchor in current.select('a[href*="page="]'):
        try:
            url = official_url(base, anchor["href"])
            query = parse_qs(urlsplit(url).query)
            if urlsplit(url).path == urlsplit(base).path and query.get("page") == [str(number + 1)]:
                return url
        except ValueError:
            continue
    return None


class CagClient:
    def __init__(self, delay: float = 1.0):
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=False)
        self.delay = max(1.0, delay)
        self.last_request = 0.0
        self.robots = None

    def close(self):
        self.client.close()

    def get(self, url: str) -> httpx.Response:
        # Stream both HTML and PDFs so response-size limits apply before buffering.
        for _ in range(6):
            url = official_url(START_URL, url)
            if self.robots and not self.robots.can_fetch(USER_AGENT, url):
                raise PermissionError(f"robots.txt disallows {url}")
            remaining = self.delay - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
            self.last_request = time.monotonic()
            response = self.client.send(self.client.build_request("GET", url), stream=True)
            if response.is_redirect:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise ValueError("Redirect without Location")
                url = official_url(url, location)
                continue
            return response
        raise RuntimeError("Too many redirects")

    def small_text(self, response: httpx.Response) -> str:
        try:
            response.raise_for_status()
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data) > 5 * 1024 * 1024:
                    raise ValueError("HTML/robots response exceeds 5 MiB")
            return data.decode("utf-8", errors="replace")
        finally:
            response.close()

    def check_robots(self) -> None:
        response = self.get("https://cag.gov.in/robots.txt")
        if response.status_code in (404, 410):
            response.close()
            LOG.info("No robots.txt published; using bounded, one-request-per-second crawling")
            return
        text = self.small_text(response)  # Denied/error responses stop crawling.
        if "<html" in text.lower():
            raise ValueError("robots.txt returned HTML; review manually before crawling")
        self.robots = RobotFileParser()
        self.robots.parse(text.splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(USER_AGENT) or 0)
        rate = self.robots.request_rate(USER_AGENT)
        if rate:
            self.delay = max(self.delay, rate.seconds / rate.requests)

    def download(self, report: dict, root: Path, max_bytes: int) -> dict:
        document_id = hashlib.sha256(report["source_url"].encode()).hexdigest()
        relative = Path("pdfs") / f"{document_id}.pdf"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(".part")
            response = self.get(report["source_url"])
            try:
                response.raise_for_status()
                total = 0
                with temporary.open("wb") as stream:
                    for part in response.iter_bytes():
                        total += len(part)
                        if total > max_bytes:
                            raise ValueError("PDF exceeds configured download size cap")
                        stream.write(part)
                with temporary.open("rb") as stream:
                    if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                        raise ValueError("Download is not a PDF")
                temporary.replace(path)
            finally:
                response.close()
                temporary.unlink(missing_ok=True)
        if path.stat().st_size > max_bytes:
            raise ValueError("Cached PDF exceeds configured size cap")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            if not stream.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("Cached file is not a PDF")
            stream.seek(0)
            for part in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(part)
        return {**report, "document_id": document_id, "pdf_path": relative.as_posix(),
                "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def crawl(root: Path, limit: int = 3, max_pages: int = 3, max_pdf_mb: int = 50) -> list[dict]:
    client = CagClient()
    downloaded, errors, seen = [], [], set()
    try:
        client.check_robots()
        url = START_URL
        for _ in range(max_pages):
            html = client.small_text(client.get(url))
            candidates = report_links(html, url)
            if not candidates:
                raise RuntimeError("No report cards found; site markup may have changed")
            for report in candidates:
                if report["source_url"] in seen:
                    continue
                seen.add(report["source_url"])
                LOG.info("Downloading %s", report["title"])
                try:
                    downloaded.append(client.download(report, root, max_pdf_mb * 1024 * 1024))
                    write_jsonl(root / "documents.jsonl", downloaded)
                except PermissionError:
                    raise
                except (httpx.HTTPError, ValueError, OSError) as error:
                    errors.append({"url": report["source_url"], "error": str(error)})
                    LOG.warning("Skipped PDF: %s", error)
                if len(downloaded) >= limit:
                    break
            if len(downloaded) >= limit:
                break
            url = next_listing(html, url)
            if not url:
                break
    finally:
        client.close()
        write_jsonl(root / "download-errors.jsonl", errors)
    if len(downloaded) < limit:
        raise RuntimeError(f"Only {len(downloaded)}/{limit} PDFs downloaded; inspect download-errors.jsonl")
    return downloaded