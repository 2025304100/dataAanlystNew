"""Operator CLI refuses unapproved targets before opening any connection."""
import pytest

pytestmark = pytest.mark.whitebox


@pytest.mark.parametrize("database", ["gpfx", "gpfx_validation", "mysql", "other", "codex_combination_"])
def test_recovery_cli_refuses_non_isolated_database_names(database, capsys, monkeypatch):
    from scripts.recover_security_status_timestamps import main
    monkeypatch.setenv("STATUS_RECOVERY_DATABASE_URL", f"mysql+pymysql://nobody:secret@invalid:3306/{database}")
    assert main(["--expected-database", database, "--original-request", "missing.json", "--content-sha256", "a" * 64]) == 2
    output = capsys.readouterr().out
    assert "secret" not in output
    assert "isolated" in output


def test_apply_requires_preview_and_explicit_confirmation_before_any_connection(capsys, monkeypatch):
    from scripts.recover_security_status_timestamps import main
    monkeypatch.setenv("STATUS_RECOVERY_DATABASE_URL", "mysql+pymysql://nobody:secret@invalid:3306/codex_combination_ci")
    assert main(["--apply", "--expected-database", "codex_combination_ci", "--original-request", "missing.json", "--content-sha256", "a" * 64]) == 2
    assert "confirmation" in capsys.readouterr().out


def test_recovery_cli_has_no_application_config_fallback(capsys, monkeypatch):
    from scripts.recover_security_status_timestamps import main
    monkeypatch.delenv("STATUS_RECOVERY_DATABASE_URL", raising=False)
    assert main(["--expected-database", "codex_combination_ci", "--original-request", "missing.json", "--content-sha256", "a" * 64]) == 2
    assert "explicit" in capsys.readouterr().out
