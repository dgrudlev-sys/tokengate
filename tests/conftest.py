import pytest


@pytest.fixture(autouse=True)
def _private_key_store(tmp_path, monkeypatch):
    """Never read or write the developer's real tokengate key store during tests."""
    from tokengate import keys
    monkeypatch.setenv("TOKENGATE_HOME", str(tmp_path / "tokengate-home"))
    keys._cache.clear()
