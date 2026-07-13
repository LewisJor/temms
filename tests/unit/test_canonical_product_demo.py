import importlib.util
import shlex
from pathlib import Path


def _load_canonical_product_demo():
    script_path = Path(__file__).parents[2] / "scripts" / "canonical_product_demo.py"
    spec = importlib.util.spec_from_file_location("canonical_product_demo", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_daemon_config_and_start_command_pin_seeded_edge(tmp_path):
    demo = _load_canonical_product_demo()

    config_path = demo._write_demo_daemon_config(tmp_path)
    command = demo._demo_daemon_start_command(config_path)
    argv = shlex.split(command)

    assert config_path == tmp_path / "temms-demo.yaml"
    config = config_path.read_text(encoding="utf-8")
    assert f'"{tmp_path / "edge" / "temms.db"}"' in config
    assert f'"{tmp_path / "edge" / "models"}"' in config
    assert f'"{tmp_path / "edge" / "policies"}"' in config
    assert "http_port: 18080" in config

    assert argv[:5] == [
        "env",
        "TEMMS_PACKAGE_SIGNING_KEY=canonical-demo-secret",
        "TEMMS_DEVICE_ID=edge-demo",
        "TEMMS_DEVICE_PROFILE=x86_64-cpu",
        "TEMMS_EDGE_HEARTBEAT_INTERVAL_S=10",
    ]
    assert argv[5:11] == ["uv", "run", "temms", "daemon", "start", "--foreground"]
    assert argv[-6:] == [
        "--host",
        "127.0.0.1",
        "--port",
        "18080",
        "--config",
        str(config_path),
    ]
