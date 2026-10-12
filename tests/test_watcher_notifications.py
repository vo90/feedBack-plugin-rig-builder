"""Notification gating preserves the existing scanner/materialization contract."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import time

import pytest


@pytest.fixture
def routes():
    path = Path(__file__).resolve().parents[1] / "routes.py"
    spec = importlib.util.spec_from_file_location("rig_notification_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Notification:
    def __init__(self, events):
        self.events = events
        self.signal = False
        self.failure = None
        self.closed = 0

    def changed(self):
        self.events.append("poll")
        if self.failure:
            raise self.failure
        if self.signal:
            self.events.append("rearm")
            self.signal = False
            return True
        return False

    def close(self):
        self.closed += 1
        self.events.append("close")


@pytest.fixture
def env(routes, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    routes._get_dlc_dir = lambda: env.root
    env = SimpleNamespace(root=root, now=0.0, events=[], handles=[], scans=0,
                          scan_effect=None, open_effect=None)

    def open_notification(path):
        env.events.append("open")
        handle = Notification(env.events)
        env.handles.append(handle)
        if env.open_effect:
            env.open_effect()
        return handle

    def scan():
        env.events.append("scan")
        env.scans += 1
        if env.scan_effect:
            return env.scan_effect()
        return routes._watch_scan_dlc()

    env.gate = routes._WatchScanGate(scan, lambda: env.root,
                                    routes._watch_directory_snapshot,
                                    lambda: bool(routes._watcher_pending),
                                    open_notification, lambda: env.now)
    env.tick = lambda: routes._watch_tick(env.gate.scan)
    return env


def put(env, name, data):
    path = env.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_arm_precedes_prime_and_unchanged_ticks_do_not_touch_state(routes, env):
    put(env, "existing.feedpak", b"old")
    assert env.tick() == []
    assert env.events == ["open", "poll", "scan"]
    assert routes._watcher_state["primed"] is True
    assert routes._watcher_sizes == {"existing.feedpak": 3}
    baseline = routes._watcher_sizes.copy()
    for instant in (5, 10, 59.9):
        env.now = instant
        assert env.tick() == []
    assert env.scans == 1
    assert routes._watcher_sizes == baseline
    assert env.events.count("rearm") == 0
    env.now = 60
    assert env.tick() == []
    assert env.scans == 2
    assert env.events.count("rearm") == 0
    env.gate.close()
    env.gate.close()
    assert env.handles[0].closed == 1


def test_signaled_changes_and_pending_confirmation_keep_original_rules(routes, env):
    stub = put(env, "nested/stub.FEEDPAK", b"")
    assert env.tick() == []
    stub.write_bytes(b"a")
    env.handles[0].signal = True
    assert env.tick() == []
    assert routes._watcher_pending == {"nested/stub.FEEDPAK": 1}
    assert env.events[-3:] == ["poll", "rearm", "scan"]
    stub.write_bytes(b"ab")  # Pending entries force a scan even without a signal.
    assert env.tick() == []
    assert env.tick() == ["nested/stub.FEEDPAK"]
    count = env.scans
    assert env.tick() == []
    assert env.scans == count
    stub.write_bytes(b"")  # A previously positive file can be re-stubbed.
    env.handles[0].signal = True
    assert env.tick() == []
    stub.write_bytes(b"new")
    env.handles[0].signal = True
    assert env.tick() == []
    stub.unlink()
    assert env.tick() == []
    assert not routes._watcher_pending
    assert not routes._watcher_sizes


def test_change_during_scan_stays_pending_for_next_tick(routes, env):
    env.tick()
    put(env, "one.feedpak", b"a")
    env.handles[0].signal = True

    def during_scan():
        result = routes._watch_scan_dlc()
        put(env, "two.feedpak", b"b")
        env.handles[0].signal = True
        return result

    env.scan_effect = during_scan
    env.tick()
    assert env.handles[0].signal
    env.scan_effect = None
    assert env.tick() == ["one.feedpak"]
    assert routes._watcher_pending == {"two.feedpak": 1}
    assert env.tick() == ["two.feedpak"]


@pytest.mark.parametrize("failure", [None, OSError("scan failed")])
def test_consumed_signal_retries_after_no_result_or_exception(routes, env, failure):
    env.tick()
    env.handles[0].signal = True
    put(env, "new.feedpak", b"new")

    def fail():
        if failure:
            raise failure
        return None

    env.scan_effect = fail
    if failure:
        with pytest.raises(OSError):
            env.tick()
    else:
        assert env.tick() == []
    assert not routes._watcher_sizes and not routes._watcher_pending
    env.scan_effect = None
    assert env.tick() == []
    assert routes._watcher_pending == {"new.feedpak": 3}
    assert env.tick() == ["new.feedpak"]


def test_reconciliation_recovers_an_unreported_change(routes, env):
    env.tick()
    put(env, "silent.feedpak", b"x")
    env.now = 59
    env.tick()
    assert env.scans == 1
    env.now = 60
    assert env.tick() == []
    assert routes._watcher_pending == {"silent.feedpak": 1}
    env.now = 65
    assert env.tick() == ["silent.feedpak"]


def test_root_change_preserves_primed_sizes_and_pending(routes, env, tmp_path):
    put(env, "pending.feedpak", b"")
    env.tick()
    put(env, "pending.feedpak", b"x")
    env.handles[0].signal = True
    env.tick()
    new_root = tmp_path / "other"
    new_root.mkdir()
    env.root = new_root
    put(env, "pending.feedpak", b"x")
    put(env, "brand-new.feedpak", b"new")
    assert env.tick() == ["pending.feedpak"]
    assert routes._watcher_pending == {"brand-new.feedpak": 3}
    assert env.handles[0].closed == 1
    assert len(env.handles) == 2
    assert env.tick() == ["brand-new.feedpak"]


def test_root_change_while_arming_closes_stale_handle_and_rearms_next_tick(env, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    env.open_effect = lambda: setattr(env, "root", other)
    env.tick()
    assert env.handles[0].closed == 1
    assert env.scans == 1
    env.open_effect = None
    env.tick()
    assert len(env.handles) == 2
    assert env.scans == 2


def test_unavailable_identity_falls_back_without_touching_pending(routes, env):
    env.tick()
    routes._watcher_pending["held.feedpak"] = 2
    original = env.root
    env.root = None
    assert env.tick() == []
    assert routes._watcher_pending == {"held.feedpak": 2}
    assert env.handles[0].closed == 1
    env.root = original
    env.tick()
    assert len(env.handles) == 2


@pytest.mark.parametrize("mode", ["unsupported", "open", "poll"])
def test_notification_failure_keeps_full_polling_until_root_changes(env, tmp_path, mode):
    if mode == "unsupported":
        env.gate._open_notification = lambda root: None
    elif mode == "open":
        env.gate._open_notification = lambda root: (_ for _ in ()).throw(OSError("open"))
    env.tick()
    if mode == "poll":
        env.handles[0].failure = OSError("wait/rearm")
    for _ in range(3):
        env.tick()
    assert env.scans == 4
    if mode == "poll":
        assert env.handles[0].closed == 1
        assert len(env.handles) == 1
        new_root = tmp_path / "new-root"
        new_root.mkdir()
        env.root = new_root
        env.tick()
        assert len(env.handles) == 2


def test_unusable_inode_never_arms_notification(env):
    env.gate._snapshot = lambda root: (root, 1, 0)
    for _ in range(3):
        env.tick()
    assert env.scans == 3
    assert not env.handles


def test_same_path_replacement_rebuilds_handle_without_repriming(routes, env):
    snapshot = routes._watch_directory_snapshot(env.root)
    inode = [snapshot[2]]
    env.gate._snapshot = lambda root: (snapshot[0], snapshot[1], inode[0])
    env.tick()
    put(env, "new.feedpak", b"x")
    inode[0] += 1
    env.tick()
    assert env.handles[0].closed == 1
    assert len(env.handles) == 2
    assert routes._watcher_pending == {"new.feedpak": 1}


def test_non_windows_never_loads_native_api(routes, monkeypatch):
    monkeypatch.setattr(routes, "os", SimpleNamespace(name="posix"))
    assert routes._WindowsDirectoryNotification.open("/tmp/library") is None


def test_close_failure_drops_only_owned_handle_and_preserves_polling(env):
    env.tick()
    handle = env.handles[0]
    handle.failure = OSError("poll failed")

    def bad_close():
        handle.closed += 1
        raise OSError("close failed")

    handle.close = bad_close
    env.tick()
    env.tick()
    env.gate.close()
    assert handle.closed == 1
    assert env.scans == 3


def test_loop_closes_owned_gate_on_stop_and_keeps_interval(routes, monkeypatch):
    actions = []

    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, seconds):
            actions.append(("wait", seconds))
            self.stopped = True

    class Condition:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def wait(self, seconds):
            routes._watcher_stop.wait(seconds)

    class Gate:
        def __init__(self, *args):
            pass
        def scan(self):
            actions.append("scan")
            return {}
        def close(self):
            actions.append("close")

    monkeypatch.setattr(routes, "_watcher_stop", Stop())
    monkeypatch.setattr(routes, "_watcher_condition", Condition())
    monkeypatch.setattr(routes, "_load_settings", lambda: {})
    monkeypatch.setattr(routes, "_WatchScanGate", Gate)
    routes._watch_loop()
    assert actions == ["scan", ("wait", 5), "close"]
    assert routes._watcher_state["running"] is False


class NativeFunction:
    def __init__(self, call):
        self.call = call
    def __call__(self, *args):
        return self.call(*args)


@pytest.mark.parametrize("stage", ["rearm", "close"])
def test_native_failure_never_rearms_timeout_or_recloses_a_handle(routes, stage):
    events = []
    notification = routes._WindowsDirectoryNotification()
    notification._handle = 0x100000001
    notification._error = lambda: OSError("native failure")
    notification._api = SimpleNamespace(
        WaitForSingleObject=lambda handle, timeout: 0,
        FindNextChangeNotification=lambda handle: events.append("rearm") or (stage != "rearm"),
        FindCloseChangeNotification=lambda handle: events.append("close") or (stage != "close"),
    )
    if stage == "rearm":
        with pytest.raises(OSError):
            notification.changed()
        notification.close()
    else:
        assert notification.changed() is True
        with pytest.raises(OSError):
            notification.close()
    notification.close()
    assert events == ["rearm", "close"]


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes declarations")
@pytest.mark.parametrize("failure", ["volume-error", "empty-volume", "invalid-handle"])
def test_native_setup_failure_has_no_owned_handle(routes, monkeypatch, failure):
    import ctypes
    events = []

    def volume(root, buffer, size):
        buffer.value = "" if failure == "empty-volume" else "C:\\"
        return failure != "volume-error"

    api = SimpleNamespace(**{name: NativeFunction(fn) for name, fn in {
        "GetVolumePathNameW": volume,
        "GetDriveTypeW": lambda root: 3,
        "FindFirstChangeNotificationW": lambda *args: ctypes.c_void_p(-1).value,
        "WaitForSingleObject": lambda *args: events.append("wait"),
        "FindNextChangeNotification": lambda *args: events.append("next"),
        "FindCloseChangeNotification": lambda *args: events.append("close"),
    }.items()})
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    if failure == "empty-volume":
        assert routes._WindowsDirectoryNotification.open("C:\\library") is None
    else:
        with pytest.raises(OSError):
            routes._WindowsDirectoryNotification.open("C:\\library")
    assert events == []


@pytest.mark.skipif(os.name != "nt", reason="Windows ctypes declarations")
@pytest.mark.parametrize("drive_type", [0, 1, 2, 3, 4, 5, 6])
def test_native_api_fixed_volume_eligibility_and_exact_rearm_rules(routes, monkeypatch, drive_type):
    import ctypes
    events = []
    waits = iter([258, 0, 258, 0xFFFFFFFF])
    handle = 0x100000001  # Regression against accidental 32-bit HANDLE truncation.

    def volume(path, buffer, size):
        events.append(("volume", path))
        buffer.value = "C:\\mounted-volume\\"
        return True

    functions = {
        "GetVolumePathNameW": volume,
        "GetDriveTypeW": lambda volume: events.append(("drive", volume)) or drive_type,
        "FindFirstChangeNotificationW": lambda *args: events.append(("first", *args)) or handle,
        "WaitForSingleObject": lambda *args: events.append(("wait", *args)) or next(waits),
        "FindNextChangeNotification": lambda h: events.append(("next", h)) or True,
        "FindCloseChangeNotification": lambda h: events.append(("close", h)) or True,
    }
    api = SimpleNamespace(**{name: NativeFunction(fn) for name, fn in functions.items()})
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api)
    notification = routes._WindowsDirectoryNotification.open("C:\\selected\\root")
    assert events[:2] == [("volume", "C:\\selected\\root"), ("drive", "C:\\mounted-volume\\")]
    if drive_type != 3:
        assert notification is None
        assert len(events) == 2
        return
    assert api.FindFirstChangeNotificationW.restype is ctypes.wintypes.HANDLE
    assert events[2] == ("first", "C:\\selected\\root", True, 0x11F)
    assert notification.changed() is False
    assert notification.changed() is True
    assert notification.changed() is False
    with pytest.raises(OSError):
        notification.changed()
    assert sum(event[0] == "next" for event in events) == 1
    assert all(event[2] == 0 for event in events if event[0] == "wait")
    notification.close()
    notification.close()
    assert [event for event in events if event[0] == "close"] == [("close", handle)]


@pytest.mark.skipif(os.name != "nt", reason="Real Windows notification contract")
def test_real_windows_disposable_library_materialization(routes, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    routes._get_dlc_dir = lambda: root
    song = root / "nested" / "stub.FEEDPAK"
    song.parent.mkdir()
    song.write_bytes(b"")
    scans = []

    def scan():
        scans.append(True)
        return routes._watch_scan_dlc()

    gate = routes._WatchScanGate(scan, lambda: root, routes._watch_directory_snapshot,
                                lambda: bool(routes._watcher_pending))

    def wait_until(predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            assert routes._watch_tick(gate.scan) == []
            time.sleep(0.01)
        assert predicate(), "Windows did not report the disposable-library change"

    def settle():
        # Notifications may coalesce or arrive just after the writing handle
        # closes. Extra full scans are safe; eventually an unchanged tick skips.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            count = len(scans)
            assert routes._watch_tick(gate.scan) == []
            if len(scans) == count:
                return
            time.sleep(0.01)
        pytest.fail("Unchanged disposable library did not settle")

    try:
        assert routes._watch_tick(gate.scan) == []
        if gate._notification is None:
            pytest.skip("Temporary filesystem does not support fixed-volume notifications")
        # Metadata from the just-created seed file can still be delivered after
        # arming; prove quiescent skipping after those notifications are drained.
        settle()
        song.write_bytes(b"n")  # In-place zero-to-positive materialization.
        wait_until(lambda: routes._watcher_pending == {"nested/stub.FEEDPAK": 1})
        song.write_bytes(b"new")  # Growth must not fire before stable confirmation.
        assert routes._watch_tick(gate.scan) == []
        assert routes._watcher_pending == {"nested/stub.FEEDPAK": 3}
        assert routes._watch_tick(gate.scan) == ["nested/stub.FEEDPAK"]
        settle()
        song.write_bytes(b"")
        wait_until(lambda: routes._watcher_sizes.get("nested/stub.FEEDPAK") == 0)
        song.write_bytes(b"again")
        wait_until(lambda: routes._watcher_pending == {"nested/stub.FEEDPAK": 5})
        song.unlink()  # Deleting a pending file cancels the confirmation.
        assert routes._watch_tick(gate.scan) == []
        assert not routes._watcher_pending
        song.write_bytes(b"recreated")
        wait_until(lambda: routes._watcher_pending == {"nested/stub.FEEDPAK": 9})
        assert routes._watch_tick(gate.scan) == ["nested/stub.FEEDPAK"]
        renamed = song.with_name("renamed.sloppak")
        song.rename(renamed)
        wait_until(lambda: routes._watcher_pending == {"nested/renamed.sloppak": 9})
        assert "nested/stub.FEEDPAK" not in routes._watcher_sizes
        assert routes._watch_tick(gate.scan) == ["nested/renamed.sloppak"]
        settle()
        renamed.unlink()
        wait_until(lambda: not routes._watcher_sizes)
        settle()
    finally:
        gate.close()
    assert gate._notification is None
