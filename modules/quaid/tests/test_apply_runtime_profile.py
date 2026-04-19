import importlib.util
import stat
from pathlib import Path


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "apply-runtime-profile.py"
    spec = importlib.util.spec_from_file_location("apply_runtime_profile", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_apply_secrets_writes_env_file_private_from_replace(tmp_path):
    mod = _load_module()
    env_file = tmp_path / ".env"
    env_file.write_text("OLD=1\n", encoding="utf-8")
    env_file.chmod(0o644)

    mod._apply_secrets(
        {
            "writeEnvFile": str(env_file),
            "env": {"TOKEN": "secret", "MODE": "test"},
        },
        tmp_path,
    )

    assert env_file.read_text(encoding="utf-8") == "TOKEN=secret\nMODE=test\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))
