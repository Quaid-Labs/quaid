from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import auth_cli


class _AdapterStub:
    def __init__(self, stored_path: Path):
        self._stored_path = stored_path
        self.shared_tokens: list[tuple[str, str]] = []

    def store_shared_auth_token(self, kind: str, token: str) -> Path:
        self.shared_tokens.append((kind, token))
        return self._stored_path

    def infer_auth_token_kind(self, token: str) -> str | None:
        if token.startswith("sk-ant-oat"):
            return "anthropic_oauth"
        if token.startswith("sk-ant-"):
            return "anthropic_api"
        if token.count(".") >= 2:
            return "codex_oauth"
        if token.startswith("sk-"):
            return "openai_api"
        return None


def test_resolve_refresh_token_from_positional_argument() -> None:
    args = SimpleNamespace(token="token-from-arg", file=None)
    assert auth_cli._resolve_refresh_token(args) == "token-from-arg"


def test_resolve_refresh_token_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "anthtoken.md"
    token_file.write_text("token-from-file\n", encoding="utf-8")

    args = SimpleNamespace(token=None, file=str(token_file))
    assert auth_cli._resolve_refresh_token(args) == "token-from-file"


def test_resolve_refresh_token_from_env_file(tmp_path: Path, monkeypatch) -> None:
    token_file = tmp_path / "anthtoken.md"
    token_file.write_text("token-from-env\n", encoding="utf-8")

    monkeypatch.setenv("QUAID_AUTH_TOKEN_FILE", str(token_file))
    args = SimpleNamespace(token=None, file=None)
    assert auth_cli._resolve_refresh_token(args) == "token-from-env"


def test_cmd_refresh_stores_token(monkeypatch, capsys, tmp_path: Path) -> None:
    adapter = _AdapterStub(tmp_path / ".auth-token")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    exit_code = auth_cli.main(["refresh", "--kind", "openai_api", "sk-openai-token"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert adapter.shared_tokens == [("openai_api", "sk-openai-token")]
    assert "Auth credential (openai_api) stored at" in captured.out


def test_main_refresh_reads_from_stdin(monkeypatch, capsys, tmp_path: Path) -> None:
    adapter = _AdapterStub(tmp_path / ".auth-token")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)
    monkeypatch.setattr(auth_cli.sys, "stdin", SimpleNamespace(read=lambda: "stdin-token\n"))

    exit_code = auth_cli.main(["refresh", "--kind", "openai_api"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert adapter.shared_tokens == [("openai_api", "stdin-token")]
    assert "Auth credential (openai_api) stored at" in captured.out


def test_cmd_refresh_infers_kind_from_token(monkeypatch, capsys, tmp_path: Path) -> None:
    adapter = _AdapterStub(tmp_path / ".auth-token")
    monkeypatch.setattr("lib.adapter.get_adapter", lambda: adapter)

    exit_code = auth_cli.main(["refresh", "sk-ant-api-token"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert adapter.shared_tokens == [("anthropic_api", "sk-ant-api-token")]
    assert "Auth credential (anthropic_api) stored at" in captured.out
