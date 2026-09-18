"""Offline-path selection must not change embedding algorithms or allow fallback downloads."""

from unittest.mock import Mock

import pytest

from ingestion import catalogue_encoder


def test_snapshot_forces_explicit_offline_model(monkeypatch, tmp_path):
    monkeypatch.setenv("CAG_MODEL_PATH", str(tmp_path))
    monkeypatch.setenv("CAG_MODEL_CACHE", str(tmp_path / "cache"))
    factory = Mock(side_effect=RuntimeError("stop-before-model-load"))
    monkeypatch.setattr(catalogue_encoder, "TextEmbedding", factory)
    with pytest.raises(RuntimeError, match="stop-before-model-load"):
        catalogue_encoder.CatalogueEncoder()
    assert factory.call_args.kwargs["specific_model_path"] == str(tmp_path)
    assert factory.call_args.kwargs["local_files_only"] is True


def test_missing_snapshot_fails_before_download(monkeypatch, tmp_path):
    monkeypatch.setenv("CAG_MODEL_PATH", str(tmp_path / "missing"))
    factory = Mock()
    monkeypatch.setattr(catalogue_encoder, "TextEmbedding", factory)
    with pytest.raises(ValueError, match="CAG_MODEL_PATH"):
        catalogue_encoder.CatalogueEncoder()
    factory.assert_not_called()


def test_scraper_default_encoder_behavior_unchanged(monkeypatch):
    monkeypatch.delenv("CAG_MODEL_PATH", raising=False)
    factory = Mock(side_effect=RuntimeError("stop-before-model-load"))
    monkeypatch.setattr(catalogue_encoder, "TextEmbedding", factory)
    with pytest.raises(RuntimeError, match="stop-before-model-load"):
        catalogue_encoder.CatalogueEncoder()
    assert "specific_model_path" not in factory.call_args.kwargs
    assert "local_files_only" not in factory.call_args.kwargs