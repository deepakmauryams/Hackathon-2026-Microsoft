"""Create a source-only VM bundle; never include local data, models or virtualenvs."""

import io
from pathlib import Path
import tarfile

DEPLOY_FILES = (
    "setup-scraper-native.sh", "cag-scraper.sh", "cag-scraper.env",
    "cag-scraper.service", "native-preflight.py", "NATIVE-SCRAPER.md",
)


def package(root: Path, output: Path):
    ingestion = root / "ingestion"
    files = sorted(ingestion.glob("*.py")) + [ingestion / "requirements.txt"]
    files += [ingestion / "deploy" / name for name in DEPLOY_FILES]
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or symlinked source file: {path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for path in files:
            # scp preserves Windows CRLF; explicitly normalize executable text.
            content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").encode("utf-8")
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return output


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    print(package(root, root / "ingestion/.cache/cag-native-worker.tar.gz"))