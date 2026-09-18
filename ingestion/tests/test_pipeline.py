from pathlib import Path

import pytest
import tiktoken

from ingestion.crawler import START_URL, next_listing, official_url, report_links
from ingestion.io import read_jsonl, write_jsonl
from ingestion.parser import TOKENIZER, chunk_pages, clean_pages


def document():
    return {"document_id": "doc1", "sha256": "hash1", "title": "Official report",
            "source_url": "https://cag.gov.in/report.pdf", "report_url": START_URL}


def test_report_cards_exclude_navigation_and_external_pdfs():
    html = '''<a href="/webroot/uploads/download_audit_report/nav.pdf">Navigation</a>
    <div class="AuditReportlisting"><div class="dateFirst"><span class="dtn">Today</span></div>
    <div class="reportIcon"><h5>Manipur</h5></div>
    <a href="/en/audit-report/details/123">State finances</a>
    <div class="pdfBottomReport"><a href="https://evil.example/report.pdf">External</a>
    <a href="/webroot/uploads/download_audit_report/full.pdf">Full PDF</a>
    <a href="/webroot/uploads/download_audit_report/duplicate.pdf">Other language</a></div></div>
    <a href="?page=2">Next</a><a href="?page=283">Last</a>'''
    reports = report_links(html, START_URL)
    assert len(reports) == 1
    assert reports[0]["title"] == "State finances"
    assert reports[0]["jurisdiction"] == "Manipur"
    assert reports[0]["source_url"].endswith("/full.pdf")
    assert next_listing(html, START_URL) == START_URL + "?page=2"


@pytest.mark.parametrize("url", ["http://cag.gov.in/a", "https://cag.gov.in.evil/a",
                                     "https://user@cag.gov.in/a", "https://cag.gov.in:444/a"])
def test_reject_nonofficial_urls(url):
    with pytest.raises(ValueError):
        official_url(START_URL, url)


def test_cleanup_only_repeated_margins():
    pages = [f"Audit report 2025\nUnique intro {i}\nKeep body\nAudit report 2025\n"
             f"Important table {i}\nPage {i + 1}" for i in range(4)]
    cleaned, removed = clean_pages(pages)
    assert "audit report 2025" in removed
    assert all(not page.startswith("Audit report") for page in cleaned)
    assert all("\nAudit report 2025\n" in page for page in cleaned)
    assert all("Page " not in page for page in cleaned)
    assert all(f"Important table {i}" in page for i, page in enumerate(cleaned))


def test_chunk_overlap_provenance_and_stable_ids():
    pages = [{"page": i + 1, "page_label": str(i - 1), "text": "Budget spending revenue. " * 140}
             for i in range(3)]
    chunks = list(chunk_pages(pages, document()))
    assert chunks == list(chunk_pages(pages, document()))
    assert chunks[0]["page"] == 1
    assert chunks[-1]["page_end"] == 3
    assert any(chunk["page_end"] > chunk["page"] for chunk in chunks)
    assert all(chunk["token_count"] == 650 for chunk in chunks[:-1])
    for left, right in zip(chunks, chunks[1:]):
        assert left["token_end"] - right["token_start"] == 100
    changed = list(chunk_pages(pages, {**document(), "sha256": "hash2"}))
    assert changed[0]["id"] != chunks[0]["id"]


@pytest.mark.parametrize("overlap", [0, 100, 499])
def test_unicode_chunks_preserve_all_tokens(overlap):
    text = "सरकारी बजट और सार्वजनिक व्यय। " * 120
    pages = [{"page": 1, "page_label": "1", "text": text}]
    chunks = list(chunk_pages(pages, document(), 500, overlap))
    assert all("\ufffd" not in chunk["text"] for chunk in chunks)
    assert all(chunk["token_count"] <= 500 for chunk in chunks)
    assert chunks[-1]["token_end"] == len(tiktoken.get_encoding(TOKENIZER).encode(text + "\n\n"))
    assert all(right["token_start"] <= left["token_end"]
               for left, right in zip(chunks, chunks[1:]))


def test_empty_and_invalid_chunks():
    assert list(chunk_pages([], document())) == []
    with pytest.raises(ValueError):
        list(chunk_pages([], document(), 650, 650))


def test_alternating_running_headers_and_source_notes():
    pages = [f"{'Chapter I: Finance' if i % 2 else 'Audit report 2025'}\n{i + 1}\n"
             f"Unique body paragraph {i}\nSource: Finance Accounts 2025" for i in range(12)]
    cleaned, removed = clean_pages(pages)
    assert "chapter i: finance" in removed and "audit report 2025" in removed
    assert all("Source: Finance Accounts 2025" in page for page in cleaned)
    assert all(f"Unique body paragraph {i}" in page for i, page in enumerate(cleaned))


@pytest.mark.parametrize("unit", ["per cent", "( i n p e r c e n t)", "Rs. in crore"])
def test_preserve_repeated_financial_units(unit):
    cleaned, removed = clean_pages([f"{unit}\nDifferent row {i}\n{unit}" for i in range(12)])
    assert not removed
    assert all(unit in page for page in cleaned)


def test_jsonl_roundtrip(tmp_path: Path):
    path = tmp_path / "nested" / "data.jsonl"
    write_jsonl(path, [{"text": "भारत", "page": 1}])
    assert read_jsonl(path) == [{"text": "भारत", "page": 1}]