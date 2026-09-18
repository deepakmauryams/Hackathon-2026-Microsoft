"""Build an allowlisted source bundle, never a copy of the working directory."""

import argparse
import io
from pathlib import Path
import tarfile

DEPLOY_FILES = (
    "package-search-api.py", "update-search-api.sh", "snapshot-search-model.py",
    "verify-search-api.py", "QDRANT-API.md",
)


def regular_source(path: Path):
    if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f"Missing or symlinked source: {path}")


def package(root: Path, output: Path):
    root, output = root.absolute(), output.absolute()
    files = sorted((root / "app").glob("*.py"))
    if not {"__init__.py", "main.py", "retrieval.py"} <= {p.name for p in files}:
        raise ValueError("Expected the complete search API source")
    files += [root / p for p in (
        "ingestion/__init__.py", "ingestion/catalogue_encoder.py", "requirements.txt",
    )]
    files += [root / "deploy" / name for name in DEPLOY_FILES]
    for path in files:
        regular_source(path)
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("Refusing a symlinked output path")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: a retry never silently replaces an existing artifact.
    with output.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for path in files:
            content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").encode()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mode = len(content), 0o644
            info.uid = info.gid = info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return output


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "ingestion/.cache/cag-search-api.tar.gz")
    print(package(root, parser.parse_args().output))