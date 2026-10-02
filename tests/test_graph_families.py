"""Every preset family x task builds a graph whose nodes and required inputs exist in the
running ComfyUI (validated against /object_info, nothing is executed)."""

import httpx
import pytest

import graphs
import presets

try:
    OBJECT_INFO = httpx.get("http://127.0.0.1:8188/object_info", timeout=10).json()
except Exception:  # ComfyUI not running
    OBJECT_INFO = None

BASE = dict(image="a.png", mask="m.png", megapixels=0.95, resolution=1008, prompt="x", negative="",
            steps=8, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=4,
            work_w=1024, work_h=1024, prefix="P")
CUSTOM_NODES = {"ViggleTurboSigmas": {"latent", "nodes"}}
CASES = [(pid, task, use_mask, mode, every)
         for pid, pr in presets.PRESETS.items() for task in pr["modes"]
         for use_mask in ([False] if task == "generate" else [False, True])
         for mode in (["inpaint"] if task == "generate" else ["inpaint", "paste"])
         for every in (0, 2)]


@pytest.mark.skipif(OBJECT_INFO is None, reason="ComfyUI not running")
@pytest.mark.parametrize("pid,task,use_mask,mode,every", CASES)
def test_graph_matches_comfy_nodes(pid, task, use_mask, mode, every):
    files = presets.resolve(pid, None)
    g = graphs.build_edit_graph(dict(BASE, **files, task=task, use_mask=use_mask, mode=mode, save_every=every,
                                     upscale=2 if every else 0, upscale_model="4x.safetensors", upscale_native=4))
    for nid, node in g.items():
        info = OBJECT_INFO.get(node["class_type"])
        if not info and node["class_type"] in CUSTOM_NODES:  # installed together with its preset
            assert set(node["inputs"]) == CUSTOM_NODES[node["class_type"]]
            continue
        assert info, f"{nid}: unknown node {node['class_type']}"
        for name, spec in info["input"].get("required", {}).items():
            if str(spec[0]).startswith("COMFY_AUTOGROW"):  # dynamic list (images.image_1, ...), may be empty
                continue
            assert name in node["inputs"], f"{nid} ({node['class_type']}) misses input {name}"
        for v in node["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int):
                assert v[0] in g, f"{nid} links to missing node {v[0]}"
    if every:
        assert g["up_fit"]["inputs"]["scale_by"] == 0.5 and "out_upscaled" in g
    if task == "generate":
        assert "load" not in g and "out_before" not in g
