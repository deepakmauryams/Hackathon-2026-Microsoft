"""Small multilingual CPU encoder; pool bounded windows without dropping text."""
import hashlib
import json
import os
from pathlib import Path
from importlib.metadata import version

import numpy as np
from fastembed import TextEmbedding
from tokenizers import Tokenizer

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSION = 384
VECTOR_NAME = "multilingual_minilm_windows_v1"


class CatalogueEncoder:
    def __init__(self):
        cache = os.environ.get("CAG_MODEL_CACHE") or str(Path(__file__).parent / ".cache" / "catalogue")
        model_path = os.environ.get("CAG_MODEL_PATH")
        options = {}
        if model_path:
            path = Path(model_path)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError("CAG_MODEL_PATH must be an existing absolute model directory")
            options = {"specific_model_path": str(path), "local_files_only": True}
        self.model = TextEmbedding(MODEL, cache_dir=cache,
                                   threads=int(os.environ.get("CAG_THREADS", "2")), **options)
        self.tokenizer = Tokenizer.from_str(self.model.model.tokenizer.to_str())
        self.tokenizer.no_padding()
        self.tokenizer.no_truncation()
        # Respect the model's configured limit; not the chunking tokenizer limit.
        self.limit = min(512, self.model.model.tokenizer.truncation["max_length"])
        root = Path(self.model.model._model_dir)
        hashes = {}
        for name in ("model_optimized.onnx", "tokenizer.json", "tokenizer_config.json", "config.json", "special_tokens_map.json"):
            path = root / name
            if path.exists():
                with path.open("rb") as stream:
                    hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        self.profile = {"model": MODEL, "dimension": DIMENSION, "context": self.limit,
            "algorithm": "token-weighted-normalized-window-mean-v1", "files": hashes,
            "fastembed": version("fastembed"), "onnxruntime": version("onnxruntime")}
        self.profile_id = hashlib.sha256(json.dumps(self.profile, sort_keys=True).encode()).hexdigest()

    def windows(self, text):
        """Split original strings, not decoded tokens; preserve Unicode and all text."""
        pending = [text]
        while pending:
            part = pending.pop()
            tokens = len(self.tokenizer.encode(part).ids)
            if tokens <= self.limit:
                yield part, max(1, tokens - self.tokenizer.num_special_tokens_to_add(False))
            else:
                if len(part) < 2:
                    raise ValueError("Cannot split overlong model input")
                middle = len(part) // 2
                pending.extend([part[middle:], part[:middle]])

    def vector(self, text):
        windows = list(self.windows(text))
        total = np.zeros(DIMENSION, dtype=np.float64)
        for start in range(0, len(windows), 4):
            batch = windows[start:start + 4]
            for (_, weight), vec in zip(batch, self.model.embed([s for s, _ in batch], batch_size=4), strict=True):
                total += vec * weight
        norm = np.linalg.norm(total)
        if not np.isfinite(total).all() or not np.isfinite(norm) or norm <= 0:
            raise ValueError("Invalid embedding")
        return (total / norm).tolist()