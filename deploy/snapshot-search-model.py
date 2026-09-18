"""Run under the existing worker Python/identity, read-only except the explicit stage.

Never export environment variables, pip config, direct_url.json, or the worker HOME.
"""

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys

MODEL_FILES = (
    "model_optimized.onnx", "tokenizer.json", "tokenizer_config.json",
    "config.json", "special_tokens_map.json",
)
REQUIRED_FILES = {"model_optimized.onnx", "tokenizer.json", "config.json"}
REQUIRED_PACKAGES = {"fastembed", "onnxruntime", "numpy", "tokenizers", "huggingface-hub", "qdrant-client"}


def installed_constraints(distributions=None):
    """Names/versions only; unlike pip freeze, this cannot export install URLs."""
    versions = {}
    for dist in metadata.distributions() if distributions is None else distributions:
        name, version = dist.metadata["Name"], dist.version
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise ValueError("Invalid installed distribution name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
            raise ValueError("Invalid installed distribution version")
        name = re.sub(r"[-_.]+", "-", name).lower()
        if name in versions and versions[name] != version:
            raise ValueError("Conflicting installed distributions")
        versions[name] = version
    if not REQUIRED_PACKAGES <= versions.keys():
        raise ValueError("Worker embedding/runtime packages are missing")
    return "".join(f"{name}=={versions[name]}\n" for name in sorted(versions))


def copy_model(root: Path, destination: Path, cache: Path):
    """Copy only model/tokenizer bytes; resolve HF blob symlinks inside the cache."""
    root, cache = root.resolve(strict=True), cache.resolve(strict=True)
    if root == cache or not root.is_relative_to(cache):
        raise ValueError("Expected the actual model root inside the worker cache, not HOME/cache itself")
    if not REQUIRED_FILES <= {name for name in MODEL_FILES if (root / name).is_file()}:
        raise ValueError("Incomplete model snapshot")
    destination.mkdir()  # Must be fresh, including on retries.
    hashes = {}
    for name in MODEL_FILES:
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        source = path.resolve(strict=True)
        if not source.is_relative_to(cache) or not source.is_file():
            raise ValueError("Model file resolves outside the worker cache or is not regular")
        target = destination / name
        # copyfile follows the resolved source into bytes, not links or metadata.
        shutil.copyfile(source, target)
        with target.open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashes


def main(stage: Path):
    if stage.is_symlink() or not stage.is_dir() or any(stage.iterdir()):
        raise ValueError("An explicitly granted, empty staging directory is required")
    # Import the installed worker code, NOT the bundled ingestion module.
    sys.path.insert(0, "/opt/cag-scraper/app")
    import ingestion.catalogue_encoder as worker

    original = worker.TextEmbedding

    def offline_embedding(*args, **kwargs):
        kwargs["local_files_only"] = True
        return original(*args, **kwargs)

    worker.TextEmbedding = offline_embedding
    encoder = worker.CatalogueEncoder()
    actual_root = Path(encoder.model.model._model_dir)
    hashes = copy_model(actual_root, stage / "model", Path(os.environ["CAG_MODEL_CACHE"]))
    if hashes != encoder.profile["files"]:
        raise ValueError("Worker model changed while copying, or uses unsupported model files")
    # Check again after copying in case a file was updated during the operation.
    for name, expected in hashes.items():
        with (actual_root / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("Worker model changed during the snapshot")
    (stage / "worker-constraints.txt").write_text(installed_constraints(), encoding="utf-8")
    (stage / "profile.json").write_text(json.dumps({
        "profile": encoder.profile, "profile_id": encoder.profile_id,
        "python": list(sys.version_info[:2]),
    }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print("Offline model bytes, embedding profile and package versions captured (no private state).")


if __name__ == "__main__":
    main(Path(sys.argv[1]))