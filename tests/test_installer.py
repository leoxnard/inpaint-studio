import zipfile

import installer
import presets


def test_patch_gguf_loader(tmp_path):
    f = tmp_path / "loader.py"
    f.write_text('IMG_ARCH_LIST = {"flux", "qwen_image"}\nx = 1\n')
    installer.patch_gguf_loader(f)
    installer.patch_gguf_loader(f)  # idempotent
    assert f.read_text().count("qwen_image21") == 1 and 'IMG_ARCH_LIST = {"flux", "qwen_image", "qwen_image21"}' in f.read_text()


def test_extract_strips_top_folder(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ComfyUI-0.38.0/main.py", "print(1)")
        zf.writestr("ComfyUI-0.38.0/comfy/x.py", "")
    installer._extract_stripped(z, tmp_path / "out")
    assert (tmp_path / "out/main.py").is_file() and (tmp_path / "out/comfy/x.py").is_file()


def test_installed_detects_models_and_desktop_yaml(tmp_path, monkeypatch):
    desk = tmp_path / "desktop"
    (desk / "instance-model-paths").mkdir(parents=True)
    models = tmp_path / "shared/models"
    (desk / "instance-model-paths/i.yaml").write_text(
        f"# comment\n#   base_path: '...'\ncomfy.desktop_0:\n  base_path: '{models}'\n  is_default: true\n")
    (tmp_path / "shared/output").mkdir(parents=True)
    monkeypatch.setattr(installer, "COMFY_DESKTOP", desk)
    monkeypatch.setattr(installer, "CONFIG_FILE", tmp_path / "none.json")
    cfg = installer.load_config()
    assert cfg["models_dir"] == str(models) and cfg["output_dir"] == str(tmp_path / "shared/output")
    (models / "unet").mkdir(parents=True)
    (models / "unet/qwen-image-2.1-UC-Q4_K_M.gguf").write_bytes(b"")
    have = installer.installed(cfg)
    assert have["model:qwen21_uc:Q4_K_M"] and not have["model:qwen21_uc:Q8_0"] and not have["component:sam3"]
    st = installer.preset_status(have, "qwen21_uc")
    assert st["installed_quants"] == ["Q4_K_M"] and not st["complete"] and st["missing_components"] == ["qwen3vl_8b", "vae_qwen21"]
    order = installer.expand(["model:qwen21_uc:Q8_0"], have)
    assert order == ["component:qwen3vl_8b", "component:vae_qwen21", "model:qwen21_uc:Q8_0"]


def test_queue_runs_items_in_order_and_accepts_more_while_running(monkeypatch):
    import asyncio

    async def scenario():
        inst = installer.Installer()
        log, gate = [], asyncio.Event()

        async def step(cfg, s, st):
            log.append(s)
            if s == "component:a":
                await gate.wait()
            if s == "component:bad":
                raise RuntimeError("boom")
            if s == "component:slow":
                await asyncio.sleep(10)

        monkeypatch.setattr(inst, "_step", step)
        monkeypatch.setattr(installer, "load_config", lambda: {})
        monkeypatch.setattr(installer, "item_title", lambda s: s)
        done = asyncio.Event()

        async def on_done():
            done.set()

        inst.start(["component:a", "component:skip"], on_done)
        await asyncio.sleep(0)
        inst.start(["component:a", "component:bad", "component:slow", "component:c"], on_done)  # a is ignored
        inst.cancel("component:skip")
        gate.set()
        while "component:slow" not in log:
            await asyncio.sleep(0)
        inst.cancel("component:slow")
        await asyncio.wait_for(done.wait(), 2)
        return log, {s: st["state"] for s, st in inst.steps.items()}, inst.error

    log, states, error = asyncio.run(scenario())
    assert log == ["component:a", "component:bad", "component:slow", "component:c"]
    assert states == {"component:a": "done", "component:skip": "cancelled", "component:bad": "error",
                      "component:slow": "cancelled", "component:c": "done"}
    assert error == "boom"


def test_loras_belong_to_a_group_of_known_families():
    families = {p["family"] for p in presets.PRESETS.values()}
    grouped = {f for fams in presets.LORA_GROUPS.values() for f in fams}
    assert grouped <= families
    loras = {k: c for k, c in presets.COMPONENTS.items() if c.get("kind") == "lora"}
    assert loras
    for key, c in loras.items():
        assert c["folder"] == "loras" and c["path"].endswith(".safetensors"), key
        assert c["families"] and set(c["families"]) <= grouped, key
        assert installer.valid_item(f"component:{key}")
