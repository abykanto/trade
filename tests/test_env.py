import importlib

from src.core import env, paths


def test_load_project_env_does_not_override_existing(monkeypatch, tmp_path):
    custom_env = tmp_path / ".env"
    custom_env.write_text("RUNTIME_DIR=from_file\n", encoding="utf-8")
    monkeypatch.setattr(env, "ENV_PATH", custom_env)
    monkeypatch.setenv("RUNTIME_DIR", "already_set")

    env.load_project_env()

    assert __import__("os").environ["RUNTIME_DIR"] == "already_set"


def test_load_project_env_sets_missing_keys(monkeypatch, tmp_path):
    custom_env = tmp_path / ".env"
    custom_env.write_text("RUNTIME_DIR=from_file\n", encoding="utf-8")
    monkeypatch.setattr(env, "ENV_PATH", custom_env)
    monkeypatch.delenv("RUNTIME_DIR", raising=False)

    env.load_project_env()

    assert __import__("os").environ["RUNTIME_DIR"] == "from_file"


def test_paths_load_from_dotenv_file(monkeypatch, tmp_path):
    runtime = tmp_path / "custom_runtime"
    custom_env = tmp_path / ".env"
    custom_env.write_text(f"RUNTIME_DIR={runtime}\n", encoding="utf-8")
    monkeypatch.setattr(env, "ENV_PATH", custom_env)
    for key in ("RUNTIME_DIR", "LOGS_DIR", "RUN_DIR", "PRICE_LOG_DIR", "DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)

    env.load_project_env()
    importlib.reload(paths)

    assert paths.RUNTIME_DIR == runtime.resolve()
    assert paths.LOGS_DIR == (runtime / "logs").resolve()
