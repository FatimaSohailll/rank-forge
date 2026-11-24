"""Deployment entry point, asset packaging, and production-only settings guards."""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import Settings

ROOT = Path(__file__).resolve().parents[1]


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


def test_vercel_defaults_and_explicit_url_precedence():
    s = settings(vercel=True, vercel_project_production_url='rankforge.vercel.app',
                 database_url='postgresql://test', session_secret='test-secret')
    assert s.public_site_url == 'https://rankforge.vercel.app'
    assert s.origins == ['https://rankforge.vercel.app']
    assert s.cookie_secure
    s.validate_deployment()
    s = settings(vercel=True, vercel_project_production_url='rankforge.vercel.app',
                 public_site_url='https://example.com', allowed_origins='https://example.com',
                 database_url='postgresql://test', session_secret='test-secret')
    assert s.public_site_url == 'https://example.com'
    assert s.origins == ['https://example.com']
    s.validate_deployment()


@pytest.mark.parametrize('overrides,expected', [
    ({'database_url': ''}, 'DATABASE_URL'),
    ({'session_secret': ''}, 'SESSION_SECRET'),
    ({'cookie_secure': False}, 'COOKIE_SECURE'),
    ({'public_site_url': 'http://localhost:8000'}, 'HTTPS'),
    ({'allowed_origins': 'http://localhost:8000'}, 'HTTPS'),
    ({'allowed_origins': ''}, 'ALLOWED_ORIGINS'),
])
def test_vercel_rejects_unsafe_runtime_configuration(overrides, expected):
    values = dict(vercel=True, vercel_project_production_url='rankforge.vercel.app',
                  database_url='postgresql://test', session_secret='test-secret')
    with pytest.raises(RuntimeError, match=expected):
        settings(**(values | overrides)).validate_deployment()


def test_local_development_still_allows_memory_and_http():
    s = settings(vercel=False, database_url='', session_secret='')
    s.validate_deployment()
    assert not s.cookie_secure
    assert s.public_site_url == 'http://localhost:8000'


def test_root_entrypoint_serves_templates_and_assets(monkeypatch, fake_llm):
    monkeypatch.syspath_prepend(str(ROOT))
    module = importlib.import_module('app')
    assert module.app is sys.modules['main'].app
    # Vercel's working directory is the project root, not backend/.
    monkeypatch.chdir(ROOT)
    with TestClient(module.app) as client:
        assert client.get('/health').text == 'ok'
        assert 'Good ideas deserve' in client.get('/').text
        assert client.get('/login').status_code == 200
        assert client.get('/static/app.css').status_code == 200
        assert client.get('/static/app.js').status_code == 200
        assert client.get('/app', follow_redirects=False).status_code == 303
