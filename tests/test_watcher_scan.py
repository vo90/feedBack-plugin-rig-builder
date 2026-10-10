from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def routes():
    spec = importlib.util.spec_from_file_location(
        "rig_builder_routes_for_scan_test", ROOT / "routes.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root, name, data=b"song"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _legacy_scan(routes):
    """Reference scan before root lookup/resolution was hoisted out of the loop."""
    dlc = routes._get_dlc_dir() if routes._get_dlc_dir else None
    if not dlc:
        return None
    current = {}
    try:
        for path in dlc.rglob("*"):
            if not path.is_file() or not routes._is_song_pack(path):
                continue
            try:
                key = routes._dlc_relative_song_key(path)
                if key:
                    current[key] = path.stat().st_size
            except OSError:
                continue
    except OSError:
        return None
    return current


def test_scan_matches_legacy_nested_paths_and_extension_case(routes, tmp_path):
    dlc = tmp_path / "library"
    for name, data in [
        ("artist/album/Song.FEEDPAK", b"feedpak"),
        ("artist/other/Song.sloppak", b"sloppak"),
        ("root.SLoPpak", b"x"),
        ("artist/zero.feedpak", b""),
        ("artist/notes.txt", b"unrelated"),
        ("raw.psarc", b"unsupported"),
    ]:
        _write(dlc, name, data)
    (dlc / "directory.feedpak").mkdir()
    routes._get_dlc_dir = lambda: dlc

    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {
        "artist/album/Song.FEEDPAK": 7,
        "artist/other/Song.sloppak": 7,
        "root.SLoPpak": 1,
        "artist/zero.feedpak": 0,
    }


def test_root_lookup_and_resolution_are_bounded_per_scan(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    for index in range(12):
        _write(dlc, f"nested/{index}.feedpak")
    lookups = []
    resolutions = []
    original_resolve = Path.resolve

    def get_dlc():
        lookups.append(dlc)
        return dlc

    def resolve(path, *args, **kwargs):
        if path == dlc:
            resolutions.append(path)
        return original_resolve(path, *args, **kwargs)

    routes._get_dlc_dir = get_dlc
    monkeypatch.setattr(Path, "resolve", resolve)
    assert len(routes._watch_scan_dlc()) == 12
    # One snapshot and one validation, independent of library size.
    assert len(lookups) == len(resolutions) == 2


def test_changed_root_is_used_on_the_next_pass(routes, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "artist/one.feedpak", b"one")
    _write(second, "other/two.sloppak", b"second")
    selected = first
    routes._get_dlc_dir = lambda: selected
    assert routes._watch_scan_dlc() == {"artist/one.feedpak": 3}
    selected = second
    assert routes._watch_scan_dlc() == {"other/two.sloppak": 6}


@pytest.mark.parametrize("unavailable", [False, True])
def test_root_change_during_walk_preserves_tick_state(routes, tmp_path, monkeypatch, unavailable):
    first = tmp_path / "first"
    second = tmp_path / "second"
    old_song = _write(first, "old.feedpak")
    _write(second, "new.feedpak")
    selected = first
    routes._get_dlc_dir = lambda: selected
    routes._watcher_state["primed"] = True
    routes._watcher_sizes.update({"old.feedpak": 0})
    routes._watcher_pending.update({"pending.feedpak": 4})

    def changed_walk(path, pattern):
        nonlocal selected
        assert path == first
        yield old_song
        selected = None if unavailable else second

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rglob", changed_walk)
        assert routes._watch_tick() == []
    assert routes._watcher_sizes == {"old.feedpak": 0}
    assert routes._watcher_pending == {"pending.feedpak": 4}
    selected = second
    assert routes._watch_tick() == []
    assert routes._watcher_sizes == {"new.feedpak": 4}
    assert routes._watcher_pending == {"new.feedpak": 4}
    assert routes._watch_tick() == ["new.feedpak"]


def test_outside_candidate_is_rejected_with_same_containment_as_legacy(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    inside = _write(dlc, "nested/inside.feedpak")
    # Also catch sibling paths that share the root's textual prefix.
    outside = _write(tmp_path / "library-other", "outside.feedpak")
    routes._get_dlc_dir = lambda: dlc
    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter((inside, outside)))
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"nested/inside.feedpak": 4}


def test_per_file_resolved_target_is_still_checked(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    inside = _write(dlc, "inside.feedpak")
    linked = _write(dlc, "linked.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        # Exercise the containment guard even on hosts lacking symlink rights.
        return outside if path == linked else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {inside.name: 4}


def _symlink(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Host cannot create test symlinks: {error}")


def test_file_symlink_keys_and_outside_rejection_match_legacy(routes, tmp_path):
    dlc = tmp_path / "library"
    inside = _write(dlc, "nested/inside.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    _symlink(dlc / "inside-alias.feedpak", inside)
    _symlink(dlc / "outside-alias.feedpak", outside)
    routes._get_dlc_dir = lambda: dlc
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"nested/inside.feedpak": 4}


def test_root_symlink_matches_legacy(routes, tmp_path):
    target = tmp_path / "target"
    _write(target, "nested/song.feedpak")
    alias = tmp_path / "library"
    _symlink(alias, target, directory=True)
    routes._get_dlc_dir = lambda: alias
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"nested/song.feedpak": 4}


def test_root_target_change_during_walk_is_unavailable(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    song = _write(dlc, "song.feedpak")
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    routes._get_dlc_dir = lambda: dlc
    original_resolve = Path.resolve
    retargeted = False

    def walk(path, pattern):
        nonlocal retargeted
        yield song
        retargeted = True

    def resolve(path, *args, **kwargs):
        if path == dlc and retargeted:
            return replacement
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", walk)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() is None


@pytest.mark.parametrize("failed_resolution", [1, 2])
def test_unavailable_root_and_resolution_errors_do_not_publish_partial_scan(
    routes, tmp_path, monkeypatch, failed_resolution
):
    dlc = tmp_path / "library"
    _write(dlc, "song.feedpak")
    routes._get_dlc_dir = lambda: None
    assert routes._watch_scan_dlc() is None
    routes._get_dlc_dir = lambda: dlc

    original_resolve = Path.resolve
    root_resolutions = 0

    def inaccessible(path, *args, **kwargs):
        nonlocal root_resolutions
        if path == dlc:
            root_resolutions += 1
            if root_resolutions == failed_resolution:
                raise OSError("root is unavailable")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", inaccessible)
    assert routes._watch_scan_dlc() is None


def test_per_file_resolution_error_skips_only_that_candidate(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    _write(dlc, "good.feedpak")
    broken = _write(dlc, "broken.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == broken:
            raise OSError("candidate disappeared")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"good.feedpak": 4}


def test_materialization_growth_re_stubbing_and_removal_keep_existing_stability_rules(routes, tmp_path):
    dlc = tmp_path / "library"
    stub = _write(dlc, "nested/stub.feedpak", b"")
    _write(dlc, "existing.sloppak", b"existing")
    routes._get_dlc_dir = lambda: dlc
    assert routes._watch_tick() == []  # Startup is a baseline, not a mass import.
    stub.write_bytes(b"a")
    assert routes._watch_tick() == []
    stub.write_bytes(b"ab")
    assert routes._watch_tick() == []  # Still growing.
    assert routes._watch_tick() == ["nested/stub.feedpak"]
    assert routes._watch_tick() == []  # Fires only once.
    new = _write(dlc, "new.FEEDPAK", b"n")
    assert routes._watch_tick() == []
    new.write_bytes(b"")
    assert routes._watch_tick() == []  # Re-stubbing cancels pending stability.
    assert "new.FEEDPAK" not in routes._watcher_pending
    new.write_bytes(b"new")
    assert routes._watch_tick() == []
    new.unlink()
    assert routes._watch_tick() == []
    assert "new.FEEDPAK" not in routes._watcher_pending
    _write(dlc, "new.FEEDPAK", b"new")
    assert routes._watch_tick() == []
    assert routes._watch_tick() == ["new.FEEDPAK"]
