"""Portable deployment contracts; no shell, systemd, network, VM or model required."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "deploy" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def builder():
    return load_helper("package-search-api")


@pytest.fixture
def source_tree(tmp_path, builder):
    root = tmp_path / "repo"
    names = ["app/__init__.py", "app/main.py", "app/retrieval.py",
             "ingestion/__init__.py", "ingestion/catalogue_encoder.py", "requirements.txt"]
    names += [f"deploy/{name}" for name in builder.DEPLOY_FILES]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"# source\r\n# normalized\r\n")
    for name in [".env", "api.env", ".venv/token", "app/__pycache__/main.pyc",
                 "app/private.env", "ingestion/data/points.jsonl", "ingestion/.cache/token",
                 "ingestion/catalogue-data/state.sqlite", "ingestion/models/model.onnx",
                 "ingestion/catalogue.py", "ingestion/deploy/cag-scraper.env",
                 "deploy/setup.sh", "deploy/hackathon-api.service", "README.md"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_OR_UNRELATED", encoding="utf-8")
    return root, set(names)


def test_bundle_exact_allowlist_and_lf(builder, source_tree, tmp_path):
    root, expected = source_tree
    output = builder.package(root, tmp_path / "bundle.tar.gz")
    with tarfile.open(output) as archive:
        assert set(archive.getnames()) == expected
        for member in archive:
            assert member.isfile() and member.mode == 0o644
            assert member.uid == member.gid == member.mtime == 0
            content = archive.extractfile(member).read()
            assert b"\r" not in content
            assert b"PRIVATE_OR_UNRELATED" not in content


def test_real_repo_bundle_does_not_include_private_state(builder, tmp_path):
    output = builder.package(ROOT, tmp_path / "actual.tar.gz")
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
    assert {n for n in names if n.startswith("ingestion/")} == {
        "ingestion/__init__.py", "ingestion/catalogue_encoder.py",
    }
    assert {n for n in names if n.startswith("deploy/")} == {f"deploy/{n}" for n in builder.DEPLOY_FILES}
    assert "requirements.txt" in names
    assert not any(n.endswith(".env") or "/.venv/" in n or "/models/" in n for n in names)


def test_bundle_does_not_overwrite_existing_file(builder, source_tree, tmp_path):
    output = tmp_path / "existing.tar.gz"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        builder.package(source_tree[0], output)
    assert output.read_bytes() == b"keep"


def make_symlink(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not available for this local identity")


def test_bundle_refuses_symlinked_source(builder, source_tree, tmp_path):
    root, _ = source_tree
    target = tmp_path / "private.py"
    target.write_text("private", encoding="utf-8")
    source = root / "app/main.py"
    source.unlink()
    make_symlink(source, target)
    output = tmp_path / "out.tar.gz"
    with pytest.raises(ValueError, match="symlinked source"):
        builder.package(root, output)
    assert not output.exists()


def test_bundle_refuses_symlinked_output_parent(builder, source_tree, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "linked"
    make_symlink(link, target, directory=True)
    with pytest.raises(ValueError, match="output path"):
        builder.package(source_tree[0], link / "out.tar.gz")
    assert not list(target.iterdir())


def test_bundle_missing_source_creates_no_archive(builder, source_tree, tmp_path):
    (source_tree[0] / "ingestion/catalogue_encoder.py").unlink()
    output = tmp_path / "out.tar.gz"
    with pytest.raises(ValueError):
        builder.package(source_tree[0], output)
    assert not output.exists()


@pytest.fixture
def snapshot():
    return load_helper("snapshot-search-model")


def test_snapshot_copies_only_model_files(snapshot, tmp_path):
    cache = tmp_path / "private-home"
    root = cache / "models--cag/snapshots/revision"
    root.mkdir(parents=True)
    for name in snapshot.MODEL_FILES:
        (root / name).write_bytes(name.encode())
    (root / "token").write_bytes(b"SECRET")
    (root / "direct_url.json").write_bytes(b"SECRET_URL")
    (cache / "state.sqlite").write_bytes(b"PRIVATE_STATE")
    copied = tmp_path / "snapshot"
    hashes = snapshot.copy_model(root, copied, cache)
    assert set(hashes) == set(snapshot.MODEL_FILES)
    assert set(p.name for p in copied.iterdir()) == set(snapshot.MODEL_FILES)
    for name, digest in hashes.items():
        assert not (copied / name).is_symlink()
        assert digest == hashlib.sha256(name.encode()).hexdigest()
    with pytest.raises(FileExistsError):
        snapshot.copy_model(root, copied, cache)


def test_snapshot_resolves_model_symlinks_to_bytes(snapshot, tmp_path):
    cache = tmp_path / "cache"
    root = cache / "snapshots/revision"
    root.mkdir(parents=True)
    blob = cache / "blob"
    blob.write_bytes(b"MODEL_BYTES")
    for name in snapshot.REQUIRED_FILES:
        make_symlink(root / name, blob)
    copied = tmp_path / "snapshot"
    snapshot.copy_model(root, copied, cache)
    assert all(p.read_bytes() == b"MODEL_BYTES" and not p.is_symlink() for p in copied.iterdir())


def test_snapshot_rejects_outside_cache_links(snapshot, tmp_path):
    cache = tmp_path / "cache"
    root = cache / "snapshot"
    root.mkdir(parents=True)
    for name in snapshot.REQUIRED_FILES:
        (root / name).write_bytes(b"model")
    secret = tmp_path / "outside-token"
    secret.write_bytes(b"SECRET")
    make_symlink(root / "special_tokens_map.json", secret)
    with pytest.raises(ValueError, match="outside"):
        snapshot.copy_model(root, tmp_path / "out", cache)
    assert not (tmp_path / "out/special_tokens_map.json").exists()


def test_snapshot_never_treats_home_as_model_root(snapshot, tmp_path):
    with pytest.raises(ValueError, match="actual model root"):
        snapshot.copy_model(tmp_path, tmp_path / "out", tmp_path)


def distributions(snapshot):
    return [SimpleNamespace(metadata={"Name": name, "Download-URL": "PRIVATE_URL"}, version="1.2.3")
            for name in snapshot.REQUIRED_PACKAGES]


def test_constraints_are_only_installed_names_and_versions(snapshot):
    result = snapshot.installed_constraints(distributions(snapshot))
    assert "PRIVATE" not in result and "http" not in result
    assert set(result.splitlines()) == {f"{name}==1.2.3" for name in snapshot.REQUIRED_PACKAGES}


@pytest.mark.parametrize("field,value", [
    ("version", "1.0\n--extra-index-url https://secret.invalid"),
    ("version", "https://user:secret@private.invalid/pkg.whl"),
    ("name", "pkg\n--trusted-host secret.invalid"),
])
def test_constraints_reject_injection_without_echoing_value(snapshot, field, value):
    items = distributions(snapshot)
    if field == "version":
        items[0].version = value
    else:
        items[0].metadata["Name"] = value
    with pytest.raises(ValueError) as error:
        snapshot.installed_constraints(items)
    assert "secret" not in str(error.value)


def test_constraints_require_embedding_stack(snapshot):
    with pytest.raises(ValueError, match="missing"):
        snapshot.installed_constraints([])


@pytest.fixture
def verification(tmp_path, monkeypatch):
    helper = load_helper("verify-search-api")
    release = tmp_path / "release"
    model = release / "snapshot/model"
    model.mkdir(parents=True)
    profile = {"files": {"model_optimized.onnx": "hash"}}
    (release / "snapshot/profile.json").write_text(json.dumps({
        "profile": profile, "profile_id": "expected", "python": list(sys.version_info[:2]),
    }), encoding="utf-8")
    monkeypatch.setattr(helper, "__file__", str(release / "deploy/verify-search-api.py"))
    monkeypatch.setenv("CAG_MODEL_PATH", str(model))
    encoder = SimpleNamespace(profile=profile, profile_id="expected",
                              model=SimpleNamespace(model=SimpleNamespace(_model_dir=model)))
    calls = []

    class Backend:
        def __init__(self, encoder_factory):
            assert encoder_factory() is encoder

        def ready(self):
            calls.append("ready")
            return {"searchable_chunks": 1}

        def search(self, request):
            assert request.top_k == 1 and request.query
            calls.append("search")
            return SimpleNamespace(results=["passage"])

        def close(self):
            calls.append("close")

    monkeypatch.setitem(sys.modules, "ingestion.catalogue_encoder", SimpleNamespace(CatalogueEncoder=lambda: encoder))
    monkeypatch.setitem(sys.modules, "app.retrieval", SimpleNamespace(CatalogueSearch=Backend, SearchRequest=SimpleNamespace))
    monkeypatch.setitem(sys.modules, "app.main", SimpleNamespace(app=object()))
    # verify() prepends its release; restore sys.path after each isolated unit test.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    return helper, encoder, calls, Backend


def test_verification_readiness_and_read_only_search(verification):
    helper, _, calls, _ = verification
    helper.verify()
    assert calls == ["ready", "search", "close"]


def test_verification_profile_mismatch_fails_before_qdrant(verification):
    helper, encoder, calls, _ = verification
    encoder.profile_id = "wrong"
    with pytest.raises(RuntimeError, match="differs"):
        helper.verify()
    assert calls == []


def test_verification_requires_actual_snapshot_root(verification, tmp_path):
    helper, encoder, calls, _ = verification
    encoder.model.model._model_dir = tmp_path / "wrong-cache"
    with pytest.raises(RuntimeError, match="ignored CAG_MODEL_PATH"):
        helper.verify()
    assert calls == []


def test_verification_closes_client_on_empty_search(verification, monkeypatch):
    helper, _, calls, backend = verification
    monkeypatch.setattr(backend, "search", lambda self, request: SimpleNamespace(results=[]))
    with pytest.raises(RuntimeError, match="no published"):
        helper.verify()
    assert calls == ["ready", "close"]


def test_service_dropin_preserves_hardening_and_bounds_resources():
    script = (ROOT / "deploy/update-search-api.sh").read_text(encoding="utf-8")
    dropin = script.split('cat <<EOF | sudo tee "$RELEASE/cag-search.conf" >/dev/null\n', 1)[1].split("\nEOF", 1)[0]
    for required in [
        "DynamicUser=yes", "ProtectSystem=strict", "ProtectHome=yes", "NoNewPrivileges=yes",
        "WorkingDirectory=$RELEASE", "ExecStart=\nExecStart=$RELEASE/.venv/bin/python",
        "--host 127.0.0.1 --port 8000 --workers 1", "MemoryMax=2G", "CPUQuota=100%",
        "RequiresMountsFor=/data/files", "Requires=cag-qdrant.service",
        "StateDirectory=hackathon-api", "StateDirectoryMode=0700",
        "Environment=CAG_MODEL_PATH=$RELEASE/snapshot/model",
        "Environment=CAG_MODEL_CACHE=/var/lib/hackathon-api/models", "CAG_THREADS=1",
        "HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1", "NO_PROXY=127.0.0.1,localhost,::1",
        "QDRANT_URL=http://127.0.0.1:6333", "CAG_COLLECTION=cag_catalogue_v1",
        "ExecStartPre=$RELEASE/.venv/bin/python $RELEASE/deploy/verify-search-api.py",
        "IPAddressDeny=any", "IPAddressAllow=localhost",
        "InaccessiblePaths=/data/files/cag/worker-native /opt/cag-scraper",
    ]:
        assert required in dropin
    assert "EnvironmentFile=" not in dropin  # No reset/replacement of inherited env files.
    assert "RAG_API_KEY" not in script
    assert "sudo apt" not in script and "sudo pip" not in script and "rm -rf" not in script
    assert "--only-binary=:all:" in script and "PIP_CONFIG_FILE=/dev/null" in script


def test_trusted_path_can_inspect_root_only_environment_directory():
    script = (ROOT / "deploy/update-search-api.sh").read_text(encoding="utf-8")
    helper = script.split("trusted_path() {", 1)[1].split("\n}", 1)[0]
    for command in ['sudo test -e "$1"', 'sudo test -L "$1"',
                    'sudo readlink -f -- "$1"', 'sudo stat -c %u -- "$1"',
                    'sudo stat -c %a -- "$1"']:
        assert command in helper
    assert "cat " not in helper and "chmod " not in helper


def test_installer_switches_only_after_offline_gate_and_restores_prior_state():
    script = (ROOT / "deploy/update-search-api.sh").read_text(encoding="utf-8")
    main = script.split("main() {", 1)[1]
    preflight = main.index('"$RELEASE/.venv/bin/python" "$RELEASE/deploy/verify-search-api.py"')
    activate = main.index('atomic_dropin "$RELEASE/cag-search.conf"')
    assert preflight < main.index('"$RELEASE/previous.conf"') < main.index("ARMED=1") < activate
    assert main.index('cd /opt/cag-scraper/app') < main.index('-p User=cag-scraper')
    assert '-p "ReadWritePaths=$RELEASE/snapshot"' in main
    assert "-p RestrictAddressFamilies=AF_UNIX" in main
    assert "mountpoint -q /data/files" in main and "4294967296" in main
    assert 'sudo chown -h -R -P root:root "$RELEASE"' in main
    assert 'trap cleanup EXIT' in main and "trap 'exit 143' TERM" in main
    assert 'atomic_dropin "$RELEASE/previous.conf"' in script
    assert 'sudo rm -- "$DROPIN"' in script
    assert 'cmp -s -- "$DROPIN" "$RELEASE/cag-search.conf"' in script
    assert "--rollback" in script and "known_dropin" in script
    assert "sudo systemctl restart hackathon-api.service" in script.split("restore_previous()", 1)[1]