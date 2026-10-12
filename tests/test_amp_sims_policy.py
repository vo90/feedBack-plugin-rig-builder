"""No live runtime, library, downloads, or native audio needed."""
import importlib.util
import json
from pathlib import Path
import threading

import pytest


@pytest.fixture
def routes(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "rig_amp_policy_test", Path(__file__).resolve().parents[1] / "routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_load_settings", lambda: {})
    return module


class Policy:
    def __init__(self, enabled=False, applicable=True):
        self.state = dict(ready=True, enabled=enabled, applicable=applicable, generation=0)
        self.handler = None
    def snapshot(self):
        return self.state
    def subscribe(self, handler):
        self.handler = handler
        handler(self.state)
        return lambda: None
    def set(self, enabled):
        self.state = {**self.state, "enabled": enabled, "generation": self.state["generation"] + 1}
        self.handler(self.state)
    def context(self):
        return {"amp_sims_policy": {"snapshot": self.snapshot, "subscribe": self.subscribe}}


def test_missing_policy_and_browser_preserve_behavior_and_own_master_wins(routes):
    routes._bind_amp_sims_policy({})
    assert routes._watch_allowed()
    routes._bind_amp_sims_policy(Policy(applicable=False).context())
    assert routes._watch_allowed()
    routes._load_settings = lambda: {"rig_builder_enabled": False}
    assert not routes._watch_allowed()


def test_disabled_policy_denies_with_terminal_code_and_subscription_failure_closes(routes):
    policy = Policy()
    routes._bind_amp_sims_policy(policy.context())
    assert not routes._watch_allowed()
    assert json.loads(routes._tones_denied().body)["code"] == "amp-sims-disabled"
    policy.set(True)
    assert routes._tones_denied() is None
    routes._bind_amp_sims_policy({"amp_sims_policy": {"snapshot": lambda: 1/0}})
    assert not routes._tones_allowed()


def test_watcher_off_has_no_scan_and_resume_primes_without_backlog(routes, monkeypatch):
    policy = Policy()
    routes._bind_amp_sims_policy(policy.context())
    actions = []
    class Gate:
        def __init__(self, *args): actions.append("open")
        def scan(self):
            actions.append("scan")
            # A file materialized while off; first scan must only baseline it.
            return {"during-off.feedpak": 500}
        def close(self): actions.append("close")
    class Condition:
        waits = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def notify_all(self): pass
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                assert timeout is None
                assert actions == []
                policy.set(True)
            elif self.waits == 2:
                assert timeout == 5
                assert actions == ["open", "scan"]
                policy.set(False)
            else:
                assert timeout is None
                assert actions == ["open", "scan", "close"]
                routes._watcher_stop.set()
    monkeypatch.setattr(routes, "_WatchScanGate", Gate)
    monkeypatch.setattr(routes, "_watcher_condition", Condition())
    monkeypatch.setattr(routes, "_watch_fire", lambda name: pytest.fail("disabled-period backlog"))
    routes._watch_loop()
    assert routes._watcher_sizes == {"during-off.feedpak": 500}
    assert not routes._watcher_state["running"]


def test_disable_during_scan_discards_fire_and_closes_handle(routes, monkeypatch):
    policy = Policy(True)
    routes._bind_amp_sims_policy(policy.context())
    closed = []
    class Gate:
        def __init__(self, *args): pass
        def close(self): closed.append(True)
        def scan(self): return {}
    def tick(scan):
        policy.set(False)
        return ["must-not-seed.feedpak"]
    class Condition:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def notify_all(self): pass
        def wait(self, timeout=None):
            assert timeout is None
            assert closed == [True]
            routes._watcher_stop.set()
    monkeypatch.setattr(routes, "_WatchScanGate", Gate)
    monkeypatch.setattr(routes, "_watch_tick", tick)
    monkeypatch.setattr(routes, "_watcher_condition", Condition())
    monkeypatch.setattr(routes, "_watch_fire", lambda name: pytest.fail("late fire"))
    routes._watch_loop()


def test_real_condition_disabled_worker_wakes_for_shutdown_without_scanning(routes, monkeypatch):
    routes._bind_amp_sims_policy(Policy().context())
    entered = threading.Event()
    condition = routes._watcher_condition
    real_wait = condition.wait
    def wait(timeout=None):
        entered.set()
        return real_wait(timeout)
    monkeypatch.setattr(condition, "wait", wait)
    monkeypatch.setattr(routes, "_WatchScanGate", lambda *a: pytest.fail("off opened watcher"))
    worker = threading.Thread(target=routes._watch_loop)
    worker.start()
    try:
        assert entered.wait(1)
        assert worker.is_alive()
    finally:
        routes._stop_watcher()
        worker.join(1)
    assert not worker.is_alive()


def test_rapid_off_on_during_scan_still_replaces_gate_and_baseline(routes, monkeypatch):
    policy = Policy(True)
    routes._bind_amp_sims_policy(policy.context())
    actions = []
    class Gate:
        def __init__(self, *args): actions.append("open")
        def close(self): actions.append("close")
        def scan(self):
            actions.append("scan")
            if actions.count("scan") == 1:
                policy.set(False)
                policy.set(True)
                return {"song.feedpak": 0}
            routes._watcher_stop.set()
            return {"song.feedpak": 500}
    monkeypatch.setattr(routes, "_WatchScanGate", Gate)
    routes._watch_loop()
    assert actions == ["open", "scan", "close", "open", "scan", "close"]
    assert routes._watcher_sizes == {"song.feedpak": 500}
    assert routes._watcher_pending == {}


def test_cancellation_before_seeding_touches_no_library_or_database(routes, monkeypatch):
    monkeypatch.setattr(routes, "_load_settings", lambda: pytest.fail("cancelled seeder did work"))
    result = routes._auto_download_for_song("x.feedpak", Path("unread"), continue_if=lambda: False)
    assert result["cancelled"] is True
    assert result["processed"] == 0


def test_runtime_build_routes_refuse_before_reading_tones(routes, monkeypatch, tmp_path):
    from fastapi import FastAPI
    monkeypatch.setenv("RIG_BUILDER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(routes, "_get_conn", lambda: None)
    monkeypatch.setattr(routes, "_install_bundled_cab_irs", lambda: None)
    monkeypatch.setattr(routes, "_start_watcher", lambda: None)
    app = FastAPI()
    routes.setup(app, {"config_dir": tmp_path, "get_dlc_dir": lambda: tmp_path,
                       **Policy().context()})
    endpoints = {route.name: route.endpoint for route in app.routes}
    for name, args in [("mega_chain_for_song", ["x.feedpak"]),
                       ("native_preset_full", [1]), ("default_tone_native", []),
                       ("auto_download_song", [{"filename": "x.feedpak"}])]:
        response = endpoints[name](*args)
        assert response.status_code == 409
        assert json.loads(response.body)["code"] == "amp-sims-disabled"
