"""Own files imported in the Download Center: symlinked, registered, removed without touching the source."""
from pathlib import Path

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


def test_model_import_with_own_text_encoder_and_vae(env):
    tmp, cfg = env
    src = tmp / "src"
    src.mkdir()
    (src / "te.gguf").write_bytes(b"t" * 4)
    (src / "my_model.gguf").write_bytes(b"m" * 8)
    te = imports.add(cfg, str(src / "te.gguf"), "text_encoder")
    m = imports.add(cfg, str(src / "my_model.gguf"), "model", base="zimage_turbo", text_encoder=f"imp_{te['id']}", vae="vae_qwen")
    pr = presets.PRESETS[f"imp_{m['id']}"]
    assert pr["text_encoder"] == f"imp_{te['id']}" and pr["vae"] == "vae_qwen"
    assert presets.resolve(f"imp_{m['id']}", None)["clip"] == "te.gguf"
    with pytest.raises(imports.ImportError_):
        imports.add(cfg, str(src / "my_model.gguf"), "model", base="zimage_turbo", vae="qwen3_4b")   # not a VAE
    imports.remove(cfg, te["id"])   # falls back to the model line's text encoder
    assert presets.PRESETS[f"imp_{m['id']}"]["text_encoder"] == presets.PRESETS["zimage_turbo"]["text_encoder"]


def test_file_already_in_models_folder(env):
    tmp, cfg = env
    f = tmp / "models/text_encoders/te-q8_0.gguf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"t" * 3)
    item = imports.add(cfg, str(f), "text_encoder")
    assert f.is_file() and not f.is_symlink()
    with pytest.raises(imports.ImportError_):
        imports.add(cfg, str(f), "text_encoder")   # only once
    imports.remove(cfg, item["id"])
    assert f.is_file()   # the real file stays


def test_language_model_links_into_lmstudio(env, monkeypatch):
    tmp, cfg = env
    lms = tmp / "lmstudio"
    monkeypatch.setenv("INPAINT_STUDIO_LMSTUDIO_MODELS", str(lms))
    (tmp / "chat-Q4_K_M.gguf").write_bytes(b"g" * 4)
    item = imports.add(cfg, str(tmp / "chat-Q4_K_M.gguf"), "llm")
    link = lms / "inpaint-studio/chat-Q4_K_M/chat-Q4_K_M.gguf"
    assert link.is_symlink() and f"imp_{item['id']}" not in presets.COMPONENTS
    (tmp / "x.safetensors").write_bytes(b"s")
    with pytest.raises(imports.ImportError_):
        imports.add(cfg, str(tmp / "x.safetensors"), "llm")
    imports.remove(cfg, item["id"])
    assert not link.exists() and (tmp / "chat-Q4_K_M.gguf").exists()
    own = lms / "pub/own/own-Q4.gguf"   # already in LM Studio: listed without a link, Remove keeps it
    own.parent.mkdir(parents=True)
    own.write_bytes(b"o")
    item = imports.add(cfg, str(own), "llm")
    assert item["in_lmstudio"] and not (lms / "inpaint-studio/own-Q4").exists()
    imports.remove(cfg, item["id"])
    assert own.exists()


def test_watched_folder_imports_updates_and_follows(env, monkeypatch):
    tmp, cfg = env
    monkeypatch.setattr(imports, "WATCH_FILE", tmp / "watch.json")
    lora_dir, model_dir = tmp / "loras_elsewhere", tmp / "models_elsewhere"
    for d in (lora_dir / "sub", model_dir):
        d.mkdir(parents=True)
    (lora_dir / "sub" / "my_style_lora.safetensors").write_bytes(b"l")
    (lora_dir / "mmproj-x.gguf").write_bytes(b"m")                      # vision projector: not a model
    (model_dir / "flux-2-klein-9b-Q4_K_M.gguf").write_bytes(b"k")        # line is in the name
    (model_dir / "mystery.safetensors").write_bytes(b"x")                # line unknown
    imports.watch_save({"lora": [str(lora_dir)], "model": [str(model_dir)]})
    res = imports.sync(cfg)
    assert sorted(res["added"]) == ["flux-2-klein-9b-Q4_K_M.gguf", "my_style_lora.safetensors", "mystery.safetensors"]
    items = {i["file"]: i for i in imports.load()}
    assert items["flux-2-klein-9b-Q4_K_M.gguf"]["base"] == "flux2_klein_9b" and not items["flux-2-klein-9b-Q4_K_M.gguf"].get("check")
    assert items["mystery.safetensors"]["check"] and items["mystery.safetensors"]["base"] == presets.DEFAULT_PRESET
    assert (Path(cfg["models_dir"]) / "loras/my_style_lora.safetensors").is_symlink()
    assert imports.sync(cfg) == {"added": [], "removed": []}              # a second scan finds nothing new
    # the user says what the mystery file is
    imports.update(items["mystery.safetensors"]["id"], base="zimage_turbo")
    again = {i["file"]: i for i in imports.load()}["mystery.safetensors"]
    assert again["base"] == "zimage_turbo" and "check" not in again
    # a removed watched file is not brought back; removing the folder takes its imports with it
    imports.remove(cfg, items["my_style_lora.safetensors"]["id"], forget=True)
    assert imports.sync(cfg)["added"] == []
    imports.watch_save({"model": [str(model_dir)]})
    assert imports.sync(cfg)["removed"] == [] and len(imports.load()) == 2
    imports.watch_save({})
    assert sorted(imports.sync(cfg)["removed"]) == ["flux-2-klein-9b-Q4_K_M.gguf", "mystery.safetensors"]
    assert not imports.load()
    with pytest.raises(imports.ImportError_):
        imports.watch_save({"lora": [str(tmp / "nope")]})
