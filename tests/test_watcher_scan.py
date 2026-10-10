from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def _mark_reparse(monkeypatch, *paths):
    """Exercise non-symlink Windows reparse tags without requiring a cloud driver."""
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path in paths:
            return SimpleNamespace(
                st_mode=info.st_mode, st_size=info.st_size,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return info

    monkeypatch.setattr(Path, "lstat", lstat)


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
    _mark_reparse(monkeypatch, linked)

    def resolve(path, *args, **kwargs):
        # Exercise the containment guard even on hosts lacking symlink rights.
        return outside if path == linked else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {inside.name: 4}


def test_outside_reparse_target_is_rejected_before_target_size_stat(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    linked = _write(dlc, "linked.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_resolve, original_stat = Path.resolve, Path.stat
    _mark_reparse(monkeypatch, linked)
    target_stats = []

    def resolve(path, *args, **kwargs):
        return outside if path == linked else original_resolve(path, *args, **kwargs)

    def path_stat(path, *args, **kwargs):
        if path == linked and kwargs.get("follow_symlinks", True):
            target_stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "stat", path_stat)
    assert routes._watch_scan_dlc() == {}
    # The initial is_file() still stats the target, exactly as before, but no
    # target-size stat is performed after the containment check fails.
    assert target_stats == [linked]


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
    _mark_reparse(monkeypatch, broken)

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


@pytest.mark.parametrize("file_count", [24, 1024])
def test_ordinary_file_resolution_scales_with_parent_count_and_preserves_order(
    routes, tmp_path, monkeypatch, file_count
):
    dlc = tmp_path / "library"
    for index in range(file_count):
        _write(dlc, f"artist{index % 3}/album/{index}.feedpak", b"x" * index)
    routes._get_dlc_dir = lambda: dlc
    expected = _legacy_scan(routes)
    resolutions = []
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        resolutions.append(path)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    actual = routes._watch_scan_dlc()
    assert list(actual.items()) == list(expected.items())
    # Root plus three file parents, each resolved once and revalidated once.
    assert len(resolutions) == 8
    assert all(not routes._is_song_pack(path) for path in resolutions)


def test_file_replaced_with_reparse_after_enumeration_is_fully_resolved(
    routes, tmp_path, monkeypatch
):
    dlc = tmp_path / "library"
    song = _write(dlc, "song.feedpak")
    outside = _write(tmp_path / "outside", "secret.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_lstat, original_resolve = Path.lstat, Path.resolve
    replaced = False
    fresh_checks = []

    def walk(path, pattern):
        nonlocal replaced
        replaced = True
        yield song

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == song:
            assert replaced
            fresh_checks.append(path)
            return SimpleNamespace(st_mode=stat.S_IFREG, st_size=4,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    def resolve(path, *args, **kwargs):
        if path == song and replaced:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", walk)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == {}
    assert fresh_checks and all(path == song for path in fresh_checks)


@pytest.mark.parametrize("parent_name", ["", "nested"])
def test_replaced_directory_identity_discards_snapshot(routes, tmp_path, monkeypatch, parent_name):
    dlc = tmp_path / "library"
    parent = dlc / parent_name
    song = _write(parent, "song.feedpak")
    old_inode = parent.stat().st_ino
    routes._get_dlc_dir = lambda: dlc

    def changed_walk(path, pattern):
        yield song
        parent.rename(tmp_path / "original-directory")
        parent.mkdir()
        assert parent.stat().st_ino != old_inode

    monkeypatch.setattr(Path, "rglob", changed_walk)
    assert routes._watch_scan_dlc() is None


def test_retargeted_nested_directory_discards_snapshot(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    parent = dlc / "nested"
    song = _write(parent, "song.feedpak")
    outside = tmp_path / "outside"
    outside.mkdir()
    routes._get_dlc_dir = lambda: dlc
    original_resolve = Path.resolve
    changed = False

    def changed_walk(path, pattern):
        nonlocal changed
        yield song
        changed = True

    def resolve(path, *args, **kwargs):
        return outside if path == parent and changed else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", changed_walk)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() is None


@pytest.mark.parametrize("name", ["song.feedpak", "irrelevant.txt"])
def test_type_check_permission_error_preserves_unavailable_scan(routes, tmp_path, monkeypatch, name):
    dlc = tmp_path / "library"
    blocked = _write(dlc, name)
    routes._get_dlc_dir = lambda: dlc
    original_stat = Path.stat

    def path_stat(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("file type cannot be read")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", path_stat)
    assert routes._watch_scan_dlc() is _legacy_scan(routes) is None


def test_ordinary_files_reuse_fresh_type_metadata_after_parent_snapshot(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    root_song = _write(dlc, "root.feedpak", b"root")
    first = _write(dlc, "nested/first.feedpak", b"first")
    second = _write(dlc, "nested/second.feedpak", b"second")
    songs = (root_song, first, second)
    routes._get_dlc_dir = lambda: dlc
    calls = []
    original_stat = Path.stat

    def path_stat(path, *args, **kwargs):
        if path in songs:
            calls.append((path, kwargs.get("follow_symlinks", True)))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter(songs))
    monkeypatch.setattr(Path, "stat", path_stat)
    assert list(routes._watch_scan_dlc().items()) == [
        ("root.feedpak", 4), ("nested/first.feedpak", 5), ("nested/second.feedpak", 6),
    ]
    # Existing parent identities permit one read; a new parent needs another
    # fresh read after its identity is captured. No follow-stat for these files.
    assert calls == [(root_song, False), (first, False), (first, False), (second, False)]


def test_parent_replaced_before_first_identity_uses_new_file_metadata(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    parent = dlc / "nested"
    song = _write(parent, "song.feedpak", b"old")
    routes._get_dlc_dir = lambda: dlc
    original_snapshot = routes._watch_directory_snapshot
    replaced = False

    def snapshot(path):
        nonlocal replaced
        if path == parent and not replaced:
            parent.rename(tmp_path / "original-directory")
            _write(parent, song.name, b"replacement")
            replaced = True
        return original_snapshot(path)

    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter((song,)))
    monkeypatch.setattr(routes, "_watch_directory_snapshot", snapshot)
    assert routes._watch_scan_dlc() == {"nested/song.feedpak": 11}


def test_first_file_reparse_change_during_parent_snapshot_is_fully_resolved(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    song = _write(dlc, "nested/song.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_snapshot = routes._watch_directory_snapshot
    original_lstat, original_resolve = Path.lstat, Path.resolve
    replaced = False

    def snapshot(path):
        nonlocal replaced
        result = original_snapshot(path)
        if path == song.parent:
            replaced = True
        return result

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == song and replaced:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_size=4,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info

    def resolve(path, *args, **kwargs):
        return outside if path == song and replaced else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(routes, "_watch_directory_snapshot", snapshot)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == {}


@pytest.mark.parametrize("error", [FileNotFoundError(2, "gone"), ValueError("bad path")])
def test_initial_no_follow_failure_retains_pathlib_missing_file_behavior(routes, tmp_path, monkeypatch, error):
    dlc = tmp_path / "library"
    good = _write(dlc, "good.feedpak")
    missing = dlc / "missing.feedpak"
    routes._get_dlc_dir = lambda: dlc
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == missing:
            raise error
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter((missing, good)))
    monkeypatch.setattr(Path, "lstat", lstat)
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"good.feedpak": 4}


def test_parent_cache_preserves_case_distinct_spellings(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    upper = _write(dlc, "A/upper.feedpak")
    lower = _write(dlc, "a/lower.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        # Emulate a Windows case-sensitive directory without changing host policy.
        if str(path) in (str(upper.parent), str(lower.parent)):
            return path
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", lambda path, pattern: iter((upper, lower)))
    monkeypatch.setattr(Path, "resolve", resolve)
    assert list(routes._watch_scan_dlc()) == ["A/upper.feedpak", "a/lower.feedpak"]


def test_unavailable_parent_identity_uses_full_file_resolution(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    song = _write(dlc, "song.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_snapshot, original_resolve = routes._watch_directory_snapshot, Path.resolve

    def snapshot(path):
        canonical, device, _ = original_snapshot(path)
        return canonical, device, 0

    def resolve(path, *args, **kwargs):
        return outside if path == song else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(routes, "_watch_directory_snapshot", snapshot)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse metadata fallback")
def test_missing_reparse_attributes_uses_full_file_resolution(routes, tmp_path, monkeypatch):
    dlc = tmp_path / "library"
    song = _write(dlc, "song.feedpak")
    outside = _write(tmp_path / "outside", "external.feedpak")
    routes._get_dlc_dir = lambda: dlc
    original_resolve, original_lstat = Path.resolve, Path.lstat

    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == song:
            return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size)
        return info

    def resolve(path, *args, **kwargs):
        return outside if path == song else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", resolve)
    assert routes._watch_scan_dlc() == {}


@pytest.fixture
def junctions():
    if os.name != "nt":
        pytest.skip("Windows junction behavior")
    created = []

    def create(link, target):
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
        assert link.is_junction()
        created.append(link)
        return link

    yield create
    for link in reversed(created):
        # Remove only the junction itself, never recursively touch its target.
        if link.is_junction():
            os.rmdir(link)


def test_windows_junction_root_nested_and_outside_match_legacy(routes, tmp_path, junctions):
    target = tmp_path / "target"
    _write(target, "nested/song.feedpak")
    _write(target, "root.sloppak", b"root")
    outside = tmp_path / "outside"
    _write(outside, "external.feedpak")
    junctions(target / "inside-junction", target / "nested")
    junctions(target / "outside-junction", outside)
    root = junctions(tmp_path / "library", target)
    routes._get_dlc_dir = lambda: root
    expected = _legacy_scan(routes)
    assert list(routes._watch_scan_dlc().items()) == list(expected.items())
    assert expected == {"root.sloppak": 4, "nested/song.feedpak": 4}


def test_windows_outside_junction_keeps_inward_reparse_alias(
    routes, tmp_path, junctions, monkeypatch
):
    dlc = tmp_path / "library"
    inside = _write(dlc, "target.txt", b"inside")
    outside = tmp_path / "outside"
    _write(outside, "alias.feedpak", b"alias")
    link = junctions(dlc / "outside-junction", outside)
    alias = link / "alias.feedpak"
    routes._get_dlc_dir = lambda: dlc
    original_resolve, original_stat = Path.resolve, Path.stat
    _mark_reparse(monkeypatch, alias)

    def resolve(path, *args, **kwargs):
        return inside if path == alias else original_resolve(path, *args, **kwargs)

    def path_stat(path, *args, **kwargs):
        if path == alias and kwargs.get("follow_symlinks", True):
            return original_stat(inside, *args, **kwargs)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "stat", path_stat)
    assert routes._watch_scan_dlc() == _legacy_scan(routes) == {"target.txt": 6}


@pytest.mark.parametrize("root_alias", [False, True])
def test_windows_junction_retarget_during_scan_is_unavailable(
    routes, tmp_path, junctions, monkeypatch, root_alias
):
    target = tmp_path / "target"
    _write(target, "song.feedpak")
    replacement = tmp_path / "replacement"
    _write(replacement, "other.feedpak")
    dlc = tmp_path / "library"
    if root_alias:
        link = junctions(dlc, target)
    else:
        dlc.mkdir()
        link = junctions(dlc / "nested", target)
    song = link / "song.feedpak"
    routes._get_dlc_dir = lambda: dlc

    def changed_walk(path, pattern):
        yield song
        assert link.is_junction()
        os.rmdir(link)
        junctions(link, replacement)

    monkeypatch.setattr(Path, "rglob", changed_walk)
    assert routes._watch_scan_dlc() is None
