import sys
from types import SimpleNamespace

from lib import embeddings


class _Provider:
    def __init__(self, *, batch=True):
        self.batch = batch

    def embed(self, text):
        return [float(len(text))]

    def embed_many(self, texts):
        if not self.batch:
            raise AttributeError("batch disabled")
        return [[float(len(text))] for text in texts]


def _failing_notice_module():
    return SimpleNamespace(
        clear_pending_notices_by_source=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("notice lock failed")
        )
    )


def test_get_embedding_logs_notice_cleanup_failure(monkeypatch, caplog):
    monkeypatch.setattr(embeddings, "get_embeddings_provider", lambda: _Provider())
    monkeypatch.setitem(sys.modules, "lib.agent_notice", _failing_notice_module())

    with caplog.at_level("WARNING", logger="lib.embeddings"):
        assert embeddings.get_embedding("alpha") == [5.0]

    assert "Failed clearing pending embedding notices" in caplog.text
    assert "notice lock failed" in caplog.text


def test_get_embeddings_batch_logs_notice_cleanup_failure(monkeypatch, caplog):
    monkeypatch.setattr(embeddings, "get_embeddings_provider", lambda: _Provider())
    monkeypatch.setitem(sys.modules, "lib.agent_notice", _failing_notice_module())

    with caplog.at_level("WARNING", logger="lib.embeddings"):
        assert embeddings.get_embeddings(["alpha", "beta"]) == [[5.0], [4.0]]

    assert "Failed clearing pending embedding notices" in caplog.text
    assert "notice lock failed" in caplog.text


def test_get_embeddings_parallel_fallback_logs_notice_cleanup_failure(monkeypatch, caplog):
    provider = _Provider()
    monkeypatch.setattr(provider, "embed_many", None)
    monkeypatch.setattr(embeddings, "get_embeddings_provider", lambda: provider)
    monkeypatch.setitem(sys.modules, "lib.agent_notice", _failing_notice_module())

    with caplog.at_level("WARNING", logger="lib.embeddings"):
        assert embeddings.get_embeddings(["alpha", "beta"], max_workers=1) == [[5.0], [4.0]]

    assert "Failed clearing pending embedding notices" in caplog.text
    assert "notice lock failed" in caplog.text


def test_embedding_parallel_workers_preserves_explicit_zero(monkeypatch):
    cfg = SimpleNamespace(
        core=SimpleNamespace(
            parallel=SimpleNamespace(
                enabled=True,
                embedding_workers=0,
                task_workers={},
            )
        )
    )
    monkeypatch.setattr("config.get_config", lambda: cfg)

    assert embeddings._embedding_parallel_workers() == 1
