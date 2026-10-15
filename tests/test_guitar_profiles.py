"""Legacy calibration must remain readable for one-time profile migration."""
import importlib.util
from pathlib import Path


def test_settings_returns_saved_input_calibration_for_profile_migration(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "rig_profile_migration_test", Path(__file__).resolve().parents[1] / "routes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = {"nam_input_calibration": 2.5, "nam_chain_input_drive": 1.5}
    monkeypatch.setattr(module, "_load_settings", lambda: dict(settings))
    monkeypatch.setattr(module, "_get_conn", lambda: None)
    monkeypatch.setattr(module, "_start_watcher", lambda: None)
    monkeypatch.setattr(module, "_install_bundled_cab_irs", lambda: None)
    monkeypatch.setattr(module, "_plugin_dir", tmp_path)
    monkeypatch.setenv("RIG_BUILDER_DATA_DIR", str(tmp_path))

    class App:
        def __init__(self):
            self.handlers = {}

        def __getattr__(self, method):
            def route(path):
                def decorate(handler):
                    self.handlers[(method, path)] = handler
                    return handler
                return decorate
            return route

    app = App()
    module.setup(app, {"config_dir": tmp_path, "get_dlc_dir": lambda: tmp_path})
    response = app.handlers[("get", "/api/plugins/rig_builder/settings")]()
    assert response["nam_input_calibration"] == 2.5
    assert response["nam_chain_input_drive"] == 1.5
