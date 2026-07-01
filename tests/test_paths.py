import os

from src.core import paths


def test_runtime_paths_default_under_project_root(monkeypatch):
    monkeypatch.delenv("RUNTIME_DIR", raising=False)
    monkeypatch.delenv("LOGS_DIR", raising=False)
    monkeypatch.delenv("RUN_DIR", raising=False)
    monkeypatch.delenv("PRICE_LOG_DIR", raising=False)

    import importlib
    importlib.reload(paths)  # noqa: F821 — paths reloaded after env override

    assert paths.RUNTIME_DIR == paths.PROJECT_ROOT / "tmp"
    assert paths.LOGS_DIR == paths.RUNTIME_DIR / "logs"
    assert paths.RUN_DIR == paths.RUNTIME_DIR / "run"
    assert paths.PRICE_LOGS_DIR == paths.RUNTIME_DIR / "data" / "price_logs"


def test_runtime_paths_from_env(monkeypatch, tmp_path):
    runtime = tmp_path / "state"
    monkeypatch.setenv("RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("LOGS_DIR", raising=False)
    monkeypatch.delenv("RUN_DIR", raising=False)
    monkeypatch.delenv("PRICE_LOG_DIR", raising=False)

    import importlib
    importlib.reload(paths)  # noqa: F821 — paths reloaded after env override

    assert paths.RUNTIME_DIR == runtime.resolve()
    assert paths.LOGS_DIR == (runtime / "logs").resolve()
    assert paths.PRICE_LOGS_DIR == (runtime / "data" / "price_logs").resolve()


def test_database_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    import importlib
    importlib.reload(paths)  # noqa: F821 — paths reloaded after env override

    assert paths.default_db_url() == "sqlite:///:memory:"

    importlib.reload(paths)
