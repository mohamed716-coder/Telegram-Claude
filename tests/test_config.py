import pytest

from kdbot.config import load_config, session_file

BASE = {"API_ID": "1", "API_HASH": "h", "BOT_TOKEN": "1:a"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env file
    for k in ["SESSION_STRING", "ADMIN_IDS", "TARGET_BOT", "COMMANDS"]:
        monkeypatch.delenv(k, raising=False)
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return monkeypatch, tmp_path / "data"


def test_reads_saved_login(env):
    mp, data = env
    data.mkdir()
    (data / "user_session.txt").write_text("saved-session\n")
    cfg = load_config()
    assert cfg.session_string == "saved-session"
    assert cfg.target_bot == "@KageNoKoroshiBot"
    assert cfg.commands == ("kd", "zz", "ko")


def test_env_session_wins(env):
    mp, data = env
    mp.setenv("SESSION_STRING", "from-env")
    assert load_config().session_string == "from-env"


def test_not_logged_in(env):
    with pytest.raises(SystemExit, match="not logged in"):
        load_config()


def test_session_file_path():
    assert session_file("/app/data") == "/app/data/user_session.txt"
