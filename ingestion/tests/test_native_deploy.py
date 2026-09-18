"""Offline tests for native deployment packaging and read-only readiness gates."""

import importlib.util
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), DEPLOY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_excludes_data_and_normalizes_lines(tmp_path):
    pack = load("package-native-worker")
    root = tmp_path / "source"
    source = root / "ingestion"
    (source / "deploy").mkdir(parents=True)
    (source / "__init__.py").write_bytes(b'"""Test."""\r\n')
    (source / "requirements.txt").write_text("httpx==0.28.1\n")
    for name in pack.DEPLOY_FILES:
        (source / "deploy" / name).write_bytes(b"# source\r\n")
    for name in (".venv", "data", ".cache", "catalogue-data"):
        directory = source / name
        directory.mkdir()
        (directory / "private.py").write_text("must not ship")
    (source / ".env").write_text("must not ship")
    output = pack.package(root, tmp_path / "bundle.tar.gz")
    with tarfile.open(output) as archive:
        expected = {"ingestion/__init__.py", "ingestion/requirements.txt"}
        expected.update(f"ingestion/deploy/{name}" for name in pack.DEPLOY_FILES)
        assert set(archive.getnames()) == expected
        for member in archive.getmembers():
            assert member.isfile()
            assert b"\r" not in archive.extractfile(member).read()


@pytest.fixture
def preflight(monkeypatch, tmp_path):
    module = load("native-preflight")
    for key in ("CAG_DATA_DIR", "CAG_MODEL_CACHE", "TMPDIR"):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("CAG_MIN_FREE_BYTES", str(5 * 1024**3))
    monkeypatch.setattr(module.os.path, "ismount", lambda path: True)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=10 * 1024**3))
    monkeypatch.setattr(module.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="eng\nhin\nosd\n"))
    response = Mock()
    response.json.return_value = {"version": "1.19.0"}
    client = Mock()
    client.get.return_value = response
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(module.httpx, "Client", factory)
    return module, factory


def test_preflight_ready_without_model_download(preflight):
    module, factory = preflight
    module.check()
    factory.assert_called_once_with(trust_env=False, timeout=10)
    client = factory.return_value.__enter__.return_value
    assert [call.args[0] for call in client.get.call_args_list] == [
        "http://127.0.0.1:6333/readyz", "http://127.0.0.1:6333/"]


@pytest.mark.parametrize("failure", ["mount", "space", "reserve", "tool", "language", "remote", "version"])
def test_preflight_rejects_unsafe_runtime(preflight, monkeypatch, failure):
    module, factory = preflight
    if failure == "mount":
        monkeypatch.setattr(module.os.path, "ismount", lambda path: False)
    elif failure == "space":
        monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=5 * 1024**3))
    elif failure == "reserve":
        monkeypatch.setenv("CAG_MIN_FREE_BYTES", "1")
    elif failure == "tool":
        monkeypatch.setattr(module.shutil, "which", lambda tool: None)
    elif failure == "language":
        monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="eng\n"))
    elif failure == "remote":
        monkeypatch.setenv("QDRANT_URL", "http://public.example:6333")
    else:
        factory.return_value.__enter__.return_value.get.return_value.json.return_value = {"version": "1.17.0"}
    with pytest.raises(RuntimeError):
        module.check()


def test_service_and_cli_share_environment_and_data():
    env = dict(line.split("=", 1) for line in (DEPLOY / "cag-scraper.env").read_text().splitlines()
               if line and not line.startswith("#"))
    service = (DEPLOY / "cag-scraper.service").read_text()
    cli = (DEPLOY / "cag-scraper.sh").read_text()
    assert "EnvironmentFile=/etc/cag-scraper/worker.env" in service
    assert ". /etc/cag-scraper/worker.env" in cli
    assert '--data-dir ${CAG_DATA_DIR} run' in service
    assert '"$CAG_DATA_DIR"' in cli
    assert "User=cag-scraper" in service
    assert "MemoryMax=4G" in service
    assert "ConditionPathIsMountPoint=/data/files" in service
    assert "ReadWritePaths=/data/files/cag/worker-native" in service
    assert all(value.startswith("/data/files/cag/worker-native/") for key, value in env.items()
               if key in ("TMPDIR", "HF_HOME", "HOME", "CAG_MODEL_CACHE", "CAG_DATA_DIR"))


def test_worker_identity_switch_uses_accessible_working_directory():
    installer = (DEPLOY / "setup-scraper-native.sh").read_text()
    cli = (DEPLOY / "cag-scraper.sh").read_text()
    assert installer.index('cd "$APP/app"') < installer.index('sudo -u cag-scraper python3')
    assert cli.index("cd /opt/cag-scraper/app") < cli.index("exec sudo -u cag-scraper")


def test_os_metadata_cannot_overwrite_qdrant_version():
    import shutil
    bash = shutil.which("bash")
    if Path(r"C:\Program Files\Git\bin\bash.exe").exists():
        bash = r"C:\Program Files\Git\bin\bash.exe"
    if not bash:
        pytest.skip("Bash not installed")
    lines = (DEPLOY / "setup-qdrant-native.sh").read_text().splitlines()
    version = next(line for line in lines if line.startswith("QDRANT_VERSION="))
    os_line = next(line.strip() for line in lines if line.strip().startswith("os_id="))
    os_line = os_line.replace(". /etc/os-release", 'VERSION="24.04 LTS (Noble Numbat)"; ID=ubuntu')
    url = next(line.strip().split(" -o ")[0] for line in lines if "https://github.com/qdrant/qdrant/releases/download/" in line)
    subprocess.run([bash, "-c", "\n".join([
        "set -eu", version, os_line, "asset=qdrant-x86_64-unknown-linux-musl.tar.gz", "url=" + url,
        '[[ "$os_id" == ubuntu ]]',
        '[[ "$url" == https://github.com/qdrant/qdrant/releases/download/v1.19.0/qdrant-x86_64-unknown-linux-musl.tar.gz ]]',
    ])], check=True)