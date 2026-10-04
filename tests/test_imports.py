"""Own files imported in the Download Center: symlinked, registered, removed without touching the source."""
import pytest
from fastapi.testclient import TestClient

import imports
import installer
import presets
import server


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "FILE", tmp_path / "imports.json")
    cfg = {**installer.defaults(), "models_dir": str(tmp_path / "models")}
    monkeypatch.setattr(installer, "load_config", lambda: cfg)
    yield tmp_path, cfg
    imports.FILE.unlink(missing_ok=True)
    imports.apply()


def test_lora_and_upscaler_import_and_remove(env):
    tmp, cfg = env
    src = tmp / "src"
    src.mkdir()
    (src / "my_style_lora.safetensors").write_bytes(b"x" * 10)
    (src / "4x_Sharp.pth").write_bytes(b"y" * 5)
    lora = imports.add(cfg, str(src / "my_style_lora.safetensors"), "lora", families=["zimage"])
    up = imports.add(cfg, str(src / "4x_Sharp.pth"), imports.guess_kind("4x_Sharp.pth"))
    link = tmp / "models/loras/my_style_lora.safetensors"
    assert link.is_symlink() and link.resolve() == (src / "my_style_lora.safetensors").resolve()
    c = presets.COMPONENTS[f"imp_{lora['id']}"]
    assert c["kind"] == "lora" and c["families"] == ["zimage"]
    assert presets.COMPONENTS[f"imp_{up['id']}"]["kind"] == "upscaler" and presets.COMPONENTS[f"imp_{up['id']}"]["scale"] == 4
    assert installer.installed(cfg)[f"component:imp_{lora['id']}"]
    assert not installer.valid_item(f"component:imp_{lora['id']}")   # never downloaded
    imports.remove(cfg, lora["id"])
    assert not link.exists() and (src / "my_style_lora.safetensors").exists()
    assert f"imp_{lora['id']}" not in presets.COMPONENTS


def test_model_import_becomes_a_preset(env):
    tmp, cfg = env
    f = tmp / "my-zimage-finetune.gguf"
    f.write_bytes(b"z" * 7)
    with pytest.raises(imports.ImportError_):
        imports.add(cfg, str(f), "model")   # needs a model line
    item = imports.add(cfg, str(f), "model", title="My Z-Image", base="zimage_turbo")
    pr = presets.PRESETS[f"imp_{item['id']}"]
    assert pr["family"] == "zimage" and pr["imported"] and pr["quants"]["GGUF"]["file"] == f.name
    assert presets.resolve(f"imp_{item['id']}", None)["unet"] == f.name
    with pytest.raises(imports.ImportError_):
        imports.add(cfg, str(f), "model", base="zimage_turbo")   # already there


def test_import_routes(env):
    tmp, _ = env
    f = tmp / "notes.txt"
    f.write_text("hi")
    c = TestClient(server.app)
    assert c.get("/api/imports/guess", params={"path": str(f)}).json()["supported"] is False
    assert c.post("/api/imports", json={"path": str(f), "kind": "lora"}).status_code == 400
    assert c.delete("/api/imports/nope").status_code == 404
