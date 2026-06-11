from kurumi_proxy.config import Settings
from kurumi_proxy.db import ConnectionStore
from kurumi_proxy.router import CredentialRouter


def test_router_selects_fill_first_by_priority(tmp_path) -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "router.sqlite3"))
    store = ConnectionStore(settings)
    low = store.create_connection(name="low-priority", api_key="low", priority=50)
    high = store.create_connection(name="high-priority", api_key="high", priority=1)

    selected = CredentialRouter(store, settings).next_connection("gpt-5.5")

    assert selected is not None
    assert selected.id == high.id
    assert selected.id != low.id


def test_router_honors_model_locks(tmp_path) -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "locked.sqlite3"))
    store = ConnectionStore(settings)
    locked = store.create_connection(name="locked", api_key="locked", priority=1)
    available = store.create_connection(name="available", api_key="available", priority=2)
    store.mark_failure(locked, model="gpt-5.5", error="rate limit", category="rate_limit")

    selected = CredentialRouter(store, settings).next_connection("gpt-5.5")

    assert selected is not None
    assert selected.id == available.id
