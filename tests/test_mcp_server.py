"""The MCP server on the owner's computer: works with no profile, never guesses whose key to use."""
from datetime import datetime

import pytest

from kit import garmin, profile

mcp_server = pytest.importorskip("kit.mcp_server")
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402


@pytest.fixture
def no_profile(monkeypatch, kit_env):
    monkeypatch.delenv("KIT_PROFILE")
    monkeypatch.delenv("KIT_TODAY")
    monkeypatch.setattr(profile, "PROFILE", kit_env / "missing.toml")


def test_no_profile_uses_the_computers_time_zone(no_profile):
    """Run with uvx there's no profile.toml: 'today' must still work, in local time, metric units."""
    assert mcp_server.day("") == datetime.now().astimezone().date()
    assert mcp_server.units() == "metric"


def test_broken_profile_is_a_plain_tool_error(monkeypatch, kit_env):
    """A profile that exists but is wrong says which key, instead of a stack trace."""
    from conftest import write_profile
    monkeypatch.setenv("KIT_PROFILE", str(write_profile(kit_env / "bad.toml", **{'units = "imperial"': 'units = "x"'})))
    with pytest.raises(ToolError, match="units"):
        mcp_server.day("")


@pytest.fixture
def env_dir(monkeypatch, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "ENV_DIR", d)
    monkeypatch.delenv("KIT_ENV_FILE", raising=False)
    return d


def test_one_env_file_is_used(env_dir):
    """The normal case: setup wrote one env file, the key comes from it."""
    (env_dir / "me__kit.env").write_text("TELEGRAM_BOT_TOKEN=x\nGARMIN_PERSONAL_API_KEY=key-one\n")
    assert mcp_server.find_key() == "key-one"


def test_two_env_files_refuse_to_guess(env_dir):
    """Two kits on one computer: never read the wrong person's Garmin data."""
    (env_dir / "a__kit.env").write_text("GARMIN_PERSONAL_API_KEY=key-a\n")
    (env_dir / "b__kit.env").write_text("GARMIN_PERSONAL_API_KEY=key-b\n")
    with pytest.raises(garmin.KeyRejected) as e:
        mcp_server.find_key()
    assert "KIT_ENV_FILE" in str(e.value) and "GARMIN_PERSONAL_API_KEY" in str(e.value)


def test_kit_env_file_and_env_var_win(env_dir, monkeypatch):
    """KIT_ENV_FILE picks the file; the env var beats everything."""
    (env_dir / "a__kit.env").write_text("GARMIN_PERSONAL_API_KEY=key-a\n")
    (env_dir / "b__kit.env").write_text("GARMIN_PERSONAL_API_KEY=key-b\n")
    monkeypatch.setenv("KIT_ENV_FILE", str(env_dir / "b__kit.env"))
    assert mcp_server.find_key() == "key-b"
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "from-env")
    assert mcp_server.find_key() == "from-env"


def test_no_key_anywhere_says_how_to_add_one(env_dir):
    """No key: the error names the setup command."""
    with pytest.raises(garmin.KeyRejected, match="setup/kit.py secret GARMIN_PERSONAL_API_KEY"):
        mcp_server.find_key()
