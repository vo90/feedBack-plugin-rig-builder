from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _routes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("startup_migration_routes", ROOT / "routes.py")
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    routes._plugin_dir = tmp_path / "plugins" / "rig_builder"
    routes._config_dir = tmp_path / "config"
    routes._config_dir.mkdir()
    routes._conn = sqlite3.connect(":memory:")
    routes._conn.executescript("""
        CREATE TABLE presets (id INTEGER PRIMARY KEY, name TEXT, settings_json TEXT);
        CREATE TABLE preset_pieces (
            id INTEGER PRIMARY KEY, rs_gear_type TEXT, params_json TEXT,
            vst_path TEXT, assigned_mode TEXT, kind TEXT, file TEXT,
            vst_format TEXT, vst_state TEXT);
    """)
    routes._conn.execute("INSERT INTO presets VALUES (1, ?, ?)", (
        "__rig_builder_master_pre__", json.dumps({"bundled_primary_assigned_v27": True})))
    bundled = routes._plugin_dir / "vst" / "pedals" / "DS-1.vst3"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(routes, "_load_vst_seed_catalog", lambda: {
        "Pedal_Distortion": [{"bundled": "vst/pedals/DS-1.vst3", "format": "VST3"}]})
    monkeypatch.setattr(routes, "_gear_category", lambda gear: "cab" if gear == "Cab" else "pedal")
    return routes, bundled


def _insert(routes, path, *, mode="auto", kind="vst", gear="Pedal_Distortion"):
    return routes._conn.execute(
        "INSERT INTO preset_pieces (rs_gear_type,params_json,vst_path,assigned_mode,"
        "kind,file,vst_format,vst_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (gear, '{"drive": 37}', path, mode, kind, "old.nam", "VST3", '{"opaque":"user-capture"}'),
    ).lastrowid


def _row(routes, piece_id):
    return routes._conn.execute("SELECT * FROM preset_pieces WHERE id=?", (piece_id,)).fetchone()


def test_rehome_and_primary_migration_preserve_resolved_state_on_repeated_startup(tmp_path, monkeypatch):
    routes, bundled = _routes(tmp_path, monkeypatch)
    pid = _insert(routes, str(bundled))
    def unexpected_recompute(*args):
        pytest.fail("Already-resolved VST parameters must not be recomputed")
    monkeypatch.setattr(routes, "_compute_vst_state_for_piece", unexpected_recompute)
    routes._migrate_rehome_bundled_vst_paths()
    expected = _row(routes, pid)
    for _ in range(2):
        changes = routes._conn.total_changes
        routes._migrate_rehome_bundled_vst_paths()
        routes._migrate_legacy_vst_paths()
        routes._migrate_assign_bundled_primary_once()
        assert _row(routes, pid) == expected
        assert routes._conn.total_changes == changes
    assert expected[3] == bundled.as_posix()


@pytest.mark.parametrize("mode", ["manual", "manual_vst"])
@pytest.mark.parametrize("kind", ["nam", "vst"])
def test_valid_manual_choices_survive_startup(tmp_path, monkeypatch, mode, kind):
    routes, _ = _routes(tmp_path, monkeypatch)
    custom = tmp_path / "Custom.vst3"
    custom.mkdir()
    pid = _insert(routes, str(custom) if kind == "vst" else None, mode=mode, kind=kind)
    expected = _row(routes, pid)
    routes._migrate_assign_bundled_primary_once()
    assert _row(routes, pid) == expected


@pytest.mark.parametrize("mode,kind,path_kind", [
    ("auto", "nam", "missing"),
    ("manual_vst", "vst", "missing"),
    ("auto", "none", "bundled"),
])
def test_unresolved_and_later_added_pieces_are_still_repaired(tmp_path, monkeypatch, mode, kind, path_kind):
    routes, bundled = _routes(tmp_path, monkeypatch)
    routes._migrate_assign_bundled_primary_once()
    path = bundled if path_kind == "bundled" else tmp_path / "Removed.vst3"
    pid = _insert(routes, path.as_posix(), mode=mode, kind=kind)
    monkeypatch.setattr(routes, "_compute_vst_state_for_piece", lambda *a: '{"params":{"drive":37}}')
    routes._migrate_assign_bundled_primary_once()
    repaired = _row(routes, pid)
    assert repaired[3] == str(bundled)
    assert repaired[5:9] == ("vst", None, "VST3", '{"params":{"drive":37}}')


def test_cabinet_and_unknown_gear_are_unchanged(tmp_path, monkeypatch):
    routes, _ = _routes(tmp_path, monkeypatch)
    ids = [_insert(routes, None, gear=gear, kind="rs_ir") for gear in ("Cab", "Unknown")]
    expected = [_row(routes, pid) for pid in ids]
    routes._migrate_assign_bundled_primary_once()
    assert [_row(routes, pid) for pid in ids] == expected


def test_legacy_rename_order_and_noop_writes(tmp_path, monkeypatch):
    routes, _ = _routes(tmp_path, monkeypatch)
    paths = [
        ("/app/plugins/nam_rig_builder/vst/StandardDistortion.vst3",
         "/app/plugins/rig_builder/vst/pedals/DS-1.vst3"),
        ("/app/plugins/rig_builder/vst/pedals/StandardDistortion.vst3",
         "/app/plugins/rig_builder/vst/pedals/DS-1.vst3"),
        ("/user/Custom.vst3", "/user/Custom.vst3"),
        ("/user/StandardDistortion.vst3-extra", "/user/StandardDistortion.vst3-extra"),
        (None, None), ("", ""),
    ]
    ids = [_insert(routes, old) for old, _ in paths]
    # Duplicate paths must migrate every referencing piece.
    ids.append(_insert(routes, paths[0][0]))
    routes._migrate_legacy_vst_paths()
    assert [_row(routes, pid)[3] for pid in ids] == [new for _, new in paths] + [paths[0][1]]
    changes = routes._conn.total_changes
    routes._migrate_legacy_vst_paths()
    assert routes._conn.total_changes == changes


def test_primary_lookup_scales_with_distinct_gear_not_song_count(tmp_path, monkeypatch):
    routes, bundled = _routes(tmp_path, monkeypatch)
    for _ in range(200):
        _insert(routes, str(bundled))
    original = routes._pick_installed_primary_vst
    calls = []
    def pick(gear, known):
        calls.append(gear)
        return original(gear, known)
    monkeypatch.setattr(routes, "_pick_installed_primary_vst", pick)
    changes = routes._conn.total_changes
    routes._migrate_assign_bundled_primary_once()
    assert calls == ["Pedal_Distortion"]
    assert routes._conn.total_changes == changes
