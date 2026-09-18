"""Page-aware extraction, conservative repeated-margin cleanup, token chunking."""

from collections import Counter
import hashlib
import math
from pathlib import Path
import re
import uuid

from pypdf import PdfReader
import tiktoken

from ingestion.io import read_jsonl, write_jsonl

TOKENIZER = "cl100k_base"
PARSER_VERSION = "cag-pages-v4"


def normalize_line(line: str) -> str:
    # Keep financial amounts distinct; only standalone page counters vary safely.
    return " ".join(line.casefold().split())


def clean_pages(pages: list[str]) -> tuple[list[str], list[str]]:
    margins = Counter()
    lines_by_page = []
    for text in pages:
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        lines_by_page.append(lines)
        margins.update({normalize_line(line) for line in lines[:3] + lines[-2:] if len(line) <= 140})
    # Running headings alternate on odd/even pages and change by chapter.
    threshold = max(3, math.ceil(len(pages) * 0.03))
    repeated = {line for line, count in margins.items()
                if count >= threshold and len(line) >= 5
                and any(char.isalpha() for char in line)
                and not line.startswith(("source", "(source"))
                and not any(unit in re.sub(r"\W", "", line)
                            for unit in ("percent", "crore", "lakh", "rupee", "million", "billion"))}
    cleaned = []
    for lines in lines_by_page:
        kept = []
        for index, line in enumerate(lines):
            edge = index < 3 or index >= len(lines) - 2
            page_number = re.fullmatch(r"(?:page\s*)?\d+(?:\s*(?:of|/)\s*\d+)?", line, re.I)
            if edge and (normalize_line(line) in repeated or page_number):
                continue
            kept.append(line)
        cleaned.append("\n".join(kept))
    return cleaned, sorted(repeated)


def chunk_pages(pages: list[dict], document: dict, chunk_size: int = 650, overlap: int = 100):
    if not 500 <= chunk_size <= 800 or not 0 <= overlap < chunk_size:
        raise ValueError("Chunk size must be 500–800 and overlap smaller than chunk size")
    encoding = tiktoken.get_encoding(TOKENIZER)
    tokens, spans = [], []
    for page in pages:
        if not page["text"].strip():
            continue
        start = len(tokens)
        tokens.extend(encoding.encode(page["text"] + "\n\n", disallowed_special=()))
        spans.append((start, len(tokens), page["page"], page["page_label"]))
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        # Never split a UTF-8 sequence at a BPE boundary (important for Indic text).
        while end > start:
            try:
                text = encoding.decode_bytes(tokens[start:end]).decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise ValueError("Unable to find a valid Unicode chunk boundary")
        touched = [span for span in spans if span[0] < end and span[1] > start]
        identity = f"{document['document_id']}:{document['sha256']}:{PARSER_VERSION}:{TOKENIZER}:{chunk_size}:{overlap}:{start}:{end}"
        yield {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
            "document_id": document["document_id"], "document_sha256": document["sha256"],
            "title": document["title"], "source_url": document["source_url"],
            "report_url": document["report_url"], "jurisdiction": document.get("jurisdiction"),
            "published_date": document.get("published_date"),
            "text": text.strip(), "page": touched[0][2], "page_end": touched[-1][2],
            "page_labels": [span[3] for span in touched],
            "token_count": end - start, "token_start": start, "token_end": end,
            "tokenizer": TOKENIZER, "parser_version": PARSER_VERSION,
            "content_sha256": hashlib.sha256(text.strip().encode()).hexdigest(),
        }
        if end == len(tokens):
            break
        start = max(start + 1, end - overlap)
        while start < end:
            try:
                encoding.decode_bytes(tokens[start:end]).decode("utf-8")
                break
            except UnicodeDecodeError:
                start += 1


def parse(root: Path, chunk_size: int = 650, overlap: int = 100) -> dict:
    documents = read_jsonl(root / "documents.jsonl")
    all_pages, all_chunks, summaries = [], [], []
    for document in documents:
        path = (root / document["pdf_path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("Manifest PDF path escapes the data directory")
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != document["sha256"]:
                raise ValueError("PDF changed since download; refresh the manifest")
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("Encrypted PDF requires manual authorized handling")
        if len(reader.pages) > 2000:
            raise ValueError("PDF exceeds the 2,000-page safety cap")
        # Plain extraction includes rotated text; layout mode drops it by default.
        raw = [page.extract_text() or "" for page in reader.pages]
        cleaned, removed = clean_pages(raw)
        labels = reader.page_labels
        pages = [{"document_id": document["document_id"], "page": i + 1,
                  "page_label": labels[i], "raw_text": raw[i], "text": text,
                  "needs_ocr": len(text.strip()) < 40}
                 for i, text in enumerate(cleaned)]
        chunks = list(chunk_pages(pages, document, chunk_size, overlap))
        all_pages.extend(pages)
        all_chunks.extend(chunks)
        summaries.append({"document_id": document["document_id"], "title": document["title"],
                          "pages": len(pages), "chunks": len(chunks),
                          "low_text_pages": sum(page["needs_ocr"] for page in pages),
                          "removed_margin_patterns": removed})
    write_jsonl(root / "pages.jsonl", all_pages)
    write_jsonl(root / "chunks.jsonl", all_chunks)
    write_jsonl(root / "parse-summary.jsonl", summaries)
    if not all_chunks:
        raise ValueError("No text extracted; PDFs may need OCR")
    return {"documents": len(documents), "pages": len(all_pages), "chunks": len(all_chunks),
            "low_text_pages": sum(page["needs_ocr"] for page in all_pages)}