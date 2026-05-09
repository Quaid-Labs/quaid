from __future__ import annotations

import sys

from datastore.docsdb import docs_cli


def test_docs_cli_dispatches_list_to_docs_registry(monkeypatch) -> None:
    from datastore.docsdb import registry

    calls = []

    def fake_main() -> int:
        calls.append(sys.argv[:])
        return 0

    monkeypatch.setattr(registry, "main", fake_main)

    assert docs_cli.main(["list", "--project", "demo"]) == 0
    assert calls == [["docs_cli.py", "list", "--project", "demo"]]


def test_docs_cli_dispatches_check_and_changelog_to_docs_updater(monkeypatch) -> None:
    from datastore.docsdb import updater

    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(updater, "main", fake_main)

    assert docs_cli.main(["check", "--json"]) == 0
    assert docs_cli.main(["changelog", "--limit", "3"]) == 0
    assert calls == [["check", "--json"], ["changelog", "--limit", "3"]]


def test_docs_cli_update_without_project_routes_to_stale_updater(monkeypatch) -> None:
    from core.docs import updater

    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(updater, "main", fake_main)

    assert docs_cli.main(["update", "--apply", "--project", "demo"]) == 0
    assert docs_cli.main(["update"]) == 0
    assert calls == [
        ["update-stale", "--apply", "--project", "demo"],
        ["update-stale"],
    ]


def test_docs_cli_update_with_project_routes_to_project_docs_cli(monkeypatch) -> None:
    from core import project_docs_cli

    calls = []

    def fake_main() -> None:
        calls.append(sys.argv[:])

    monkeypatch.setattr(project_docs_cli, "main", fake_main)

    assert docs_cli.main(["update", "demo", "--json"]) == 0
    assert calls == [["docs_cli.py", "update", "demo", "--json"]]


def test_docs_cli_unknown_command_prints_usage(capsys) -> None:
    assert docs_cli.main(["unknown"]) == 1

    captured = capsys.readouterr()
    assert "Usage: quaid docs {list|check|update|changelog}" in captured.err
