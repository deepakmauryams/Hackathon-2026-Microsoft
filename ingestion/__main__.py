"""Run from the repository root: python -m ingestion --help."""

import argparse
import json
import logging
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parent / "data"


def main():
    parser = argparse.ArgumentParser(description="Standalone local CAG PDF ingestion")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("crawl", help="Download 3–5 official report PDFs")
    download.add_argument("--limit", type=int, choices=range(3, 6), default=3)
    download.add_argument("--max-pages", type=int, choices=range(1, 11), default=3)
    extract = commands.add_parser("parse", help="Extract pages and overlapping chunks")
    extract.add_argument("--chunk-size", type=int, choices=range(500, 801), default=650)
    extract.add_argument("--overlap", type=int, default=100)
    commands.add_parser("embed", help="Generate local CPU embeddings into vectors.jsonl")
    commands.add_parser("index", help="Upsert embeddings to the local Qdrant collection")
    search = commands.add_parser("search", help="Search the ingested reports (not the mock API)")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, choices=range(1, 21), default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "crawl":
        from ingestion.crawler import crawl
        result = {"documents": len(crawl(args.data_dir, args.limit, args.max_pages))}
    elif args.command == "parse":
        from ingestion.parser import parse
        result = parse(args.data_dir, args.chunk_size, args.overlap)
    else:
        from ingestion.vector_store import embed, index, search
        if args.command == "embed":
            result = embed(args.data_dir)
        elif args.command == "index":
            result = index(args.data_dir)
        else:
            result = search(args.data_dir, args.query, args.top_k)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()