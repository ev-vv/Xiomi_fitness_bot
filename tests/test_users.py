import sqlite3

from core import data, user
from extractor.config import Config
from synth import TARGET


def test_data_dir_follows_current_user(app_env, tmp_path, monkeypatch):
    monkeypatch.setenv("MIFIT_DATA_DIR", str(tmp_path))
    assert Config().data_dir == tmp_path.resolve()
    with user.use(5):
        assert Config().data_dir == tmp_path.resolve() / "users" / "5"
        assert user.key("sync:task") == "u5:sync:task"
    monkeypatch.setenv("MIFIT_USER_ID", "6")  # CLI tools pick a user from the environment
    assert Config().token_path == tmp_path.resolve() / "users" / "6" / "secrets" / "token.json"


def test_processor_cache_is_per_user(app_env, tmp_path, monkeypatch):
    for uid in (1, 2):
        (tmp_path / "users").mkdir(exist_ok=True)
        (tmp_path / "users" / str(uid)).symlink_to(app_env)
    monkeypatch.setenv("MIFIT_DATA_DIR", str(tmp_path))
    with user.use(1):
        a = data.processor()
        assert data.processor() is a
    with user.use(2):
        b = data.processor()
    assert a is not b and a.cfg.data_dir != b.cfg.data_dir


def test_tasks_run_as_the_publishing_user(app_env, tmp_path, monkeypatch):
    from worker import tasks
    from worker.celery_app import app

    (tmp_path / "users").mkdir()
    (tmp_path / "users" / "9").symlink_to(app_env)
    monkeypatch.setenv("MIFIT_DATA_DIR", str(tmp_path))
    app.conf.update(task_always_eager=True, task_store_eager_result=True, result_backend="cache+memory://")
    try:
        with user.use(9):
            res = tasks.render_dashboard.delay("sleep", "week", TARGET.isoformat()).get()
    finally:
        app.conf.update(task_always_eager=False, task_store_eager_result=False)
    assert res["status"] == "ok" and "/users/9/cache/charts/" in res["path"]


def test_migration_moves_user_files_and_rows(tmp_path):
    from core.migrate_users import move_files, namespace_db

    root = tmp_path / "data"
    for p in ("raw/heart_rate/x.parquet", "secrets/token.json", "state/extract_state.sqlite", "state/bot.sqlite",
              "warehouse.duckdb"):
        (root / p).parent.mkdir(parents=True, exist_ok=True)
        (root / p).write_text("x")
    db = root / "state" / "bot.sqlite"
    db.unlink()
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);"
                      "CREATE TABLE analyses (key TEXT PRIMARY KEY, payload TEXT, created_at TEXT);"
                      "CREATE TABLE llm_usage (id INTEGER PRIMARY KEY, cost_usd REAL);"
                      "INSERT INTO settings VALUES ('profile_sex', 'f'), ('u7:alerts', '0');"
                      "INSERT INTO analyses VALUES ('sleep:week:abc', '{}', '');"
                      "INSERT INTO llm_usage (cost_usd) VALUES (0.1);")
    con.commit()
    con.close()
    assert sorted(move_files(root, 42)) == ["raw", "secrets", "state/extract_state.sqlite", "warehouse.duckdb"]
    assert (root / "users/42/secrets/token.json").exists() and (root / "state/bot.sqlite").exists()
    assert namespace_db(db, 42) == {"settings": 1, "analyses": 1, "llm_usage": 1}
    assert move_files(root, 42) == [] and namespace_db(db, 42) == {"settings": 0, "analyses": 0, "llm_usage": 0}
    con = sqlite3.connect(db)
    assert sorted(k for (k,) in con.execute("SELECT key FROM settings")) == ["u42:profile_sex", "u7:alerts"]
    assert con.execute("SELECT user_id FROM llm_usage").fetchone() == (42,)
