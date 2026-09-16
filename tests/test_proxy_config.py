from pathlib import Path

import pytest

from deploy.configure_vm import LIMITS_INCLUDE, add_proxy_limits


def test_limits_migration_is_repeatable():
    original = (Path(__file__).parents[1] / "deploy" / "nginx.conf").read_text()
    updated = add_proxy_limits(original)
    assert updated.count(LIMITS_INCLUDE) == 1
    assert add_proxy_limits(updated) == updated
    assert "proxy_pass http://127.0.0.1:8000;" in updated


def test_tls_configuration_preserved():
    original = 'server { listen 443 ssl; server_name api.example.com; location / { proxy_pass http://127.0.0.1:8000; } }'
    updated = add_proxy_limits(original)
    assert 'listen 443 ssl; server_name api.example.com;' in updated


@pytest.mark.parametrize("config", [
    "server { return 404; }",
    "server { location / { client_max_body_size 8k; } }",
])
def test_unknown_configuration_requires_manual_review(config):
    with pytest.raises(ValueError):
        add_proxy_limits(config)