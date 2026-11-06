import pytest

from synth import make_lake


@pytest.fixture(scope="session")
def lake_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("lake")
    make_lake(root)
    return root


@pytest.fixture
def app_env(lake_dir, tmp_path, monkeypatch):
    """Points settings, analytics and storage at the synthetic lake and a fresh SQLite file."""
    from ai import client as llm
    from core import config, data
    from storage import db

    monkeypatch.setenv("MIFIT_DATA_DIR", str(lake_dir))
    monkeypatch.setenv("MIFIT_TZ", "Europe/Moscow")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.sqlite"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for cached in (config.settings, db.engine, llm.sync_client, llm.async_client):
        cached.cache_clear()
    data.reset()
    yield lake_dir
    for cached in (config.settings, db.engine, llm.sync_client, llm.async_client):
        if hasattr(cached, "cache_clear"):  # may still be monkeypatched at this point
            cached.cache_clear()
    data.reset()
