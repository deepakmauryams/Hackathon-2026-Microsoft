"""Resource-isolated, per-document extraction with bounded English/Hindi OCR."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from pypdf import PdfReader
from ingestion.io import write_jsonl
from ingestion.parser import clean_pages, chunk_pages


def reserve(path):
    if shutil.disk_usage(path).free < int(os.environ.get("CAG_MIN_FREE_BYTES", str(5 * 1024**3))):
        raise OSError("Low disk space: ingestion paused to preserve reserve")


def extract(root):
    document = json.loads((root / "document.json").read_text(encoding="utf-8"))
    pdf = root / document["pdf_path"]
    reader = PdfReader(pdf)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("Encrypted PDF")
    if len(reader.pages) > 2000:
        raise ValueError("PDF exceeds 2000 page safety limit; manual review required")
    langs = os.environ.get("CAG_OCR_LANGUAGES", "eng+hin")
    available = shutil.which("pdftoppm") and shutil.which("tesseract")
    raw, methods, failures = [], [], []
    total_characters = 0
    for i, page in enumerate(reader.pages):
        reserve(root)
        content = page.extract_text() or ""
        method = "pdf-text"
        if len(content.strip()) < 40:
            if available:
                image = root / "ocr-page.png"
                try:
                    subprocess.run(["pdftoppm", "-f", str(i + 1), "-l", str(i + 1),
                        "-scale-to", "2200", "-singlefile", "-png", str(pdf), str(root / "ocr-page")],
                        check=True, capture_output=True, timeout=120)
                    result = subprocess.run(["tesseract", str(image), "stdout", "-l", langs],
                        check=True, capture_output=True, timeout=120)
                    ocr = result.stdout.decode("utf-8", errors="replace")
                    if len(ocr.strip()) > len(content.strip()):
                        content, method = ocr, "ocr-" + langs
                except (subprocess.SubprocessError, OSError) as error:
                    failures.append({"page": i + 1, "error": str(error)[:500]})
                finally:
                    image.unlink(missing_ok=True)
            else:
                failures.append({"page": i + 1, "error": "OCR tools unavailable"})
        total_characters += len(content)
        if total_characters > 20_000_000:
            raise ValueError("PDF extracted text exceeds 20 million character safety cap; manual review required")
        raw.append(content)
        methods.append(method)
    cleaned, removed = clean_pages(raw)
    pages = [{"document_id": document["document_id"], "page": i + 1,
        "page_label": reader.page_labels[i], "text": value, "raw_text": raw[i],
        "extraction": methods[i], "needs_review": len(value.strip()) < 40}
        for i, value in enumerate(cleaned)]
    chunks = list(chunk_pages(pages, document))
    if not chunks:
        raise ValueError("No searchable text after extraction/OCR")
    for chunk in chunks:
        chunk["extraction_version"] = "catalogue-pdf-v1"
    write_jsonl(root / "chunks.jsonl", chunks)
    write_jsonl(root / "pages.jsonl", pages)
    summary = {"pages": len(pages), "chunks": len(chunks), "ocr_pages": sum(m.startswith("ocr") for m in methods),
        "low_text_pages": sum(p["needs_review"] for p in pages), "ocr_failures": failures,
        "removed_margins": removed, "coverage": "partial" if failures or any(p["needs_review"] for p in pages) else "text-extracted"}
    write_jsonl(root / "summary.jsonl", [summary])


if __name__ == "__main__":
    extract(Path(sys.argv[1]))