"""Template (Generate): a soft canny ControlNet on the template, or a whole-image edit of it for models without one."""
import asyncio

import pytest
from fastapi import HTTPException

import graphs
import installer
import presets
import server


def base(**kw):
    return {"task": "generate", "preset": "qwen21_viggle", "family": "qwen21_turbo", "prompt": "a vinyl record",
            "src_w": 400, "src_h": 400, "megapixels": 0.95, "resolution": 1008, "steps": 6, "cfg": 1.0, "seed": 1,
            "sampler": "euler", "scheduler": "simple", "denoise": 1.0, "template": {"image": "t.png", "w": 960, "h": 960},
            **kw}


def run_job(monkeypatch, params):
    seen = {}
    monkeypatch.setattr(installer, "job_missing", lambda *a, **k: [])
    monkeypatch.setattr(server, "save_run_config", lambda *a, **k: None)
    monkeypatch.setattr(server, "remember_prompt", lambda *a, **k: None)
    monkeypatch.setattr(server, "split_encode", lambda *a, **k: None, raising=False)   # only where split_encode exists
    monkeypatch.setattr(server, "start_job", lambda run, p, g: seen.update(run=run, params=p, graph=g) or run)
    asyncio.run(server.create_job(params))
    return seen


def test_template_runs_as_a_soft_canny_controlnet(monkeypatch):
    seen = run_job(monkeypatch, base())
    p = seen["params"]
    assert p["task"] == "generate" and p.get("image") is None
    assert p["control"] == {"type": "canny", "image": "t.png", "source": "photo", "strength": 0.75, "end": 0.65}
    assert p["template"] == {"image": "t.png", "w": 960, "h": 960, "strict": 0.5}
    assert any(n["class_type"] == "Canny" for n in seen["graph"].values())
    strict = run_job(monkeypatch, base(template={"image": "t.png", "w": 960, "h": 960, "strict": 1}))["params"]["control"]
    assert (strict["strength"], strict["end"]) == (1.0, 0.95)


def test_template_control_follows_the_slider():
    assert graphs.template_control(0) == (0.5, 0.35)
    assert graphs.template_control(0.5) == (0.75, 0.65)
    assert graphs.template_control(7) == (1.0, 0.95)


def test_template_without_controlnet_is_a_whole_image_edit(monkeypatch):
    monkeypatch.setattr(presets, "control_patch", lambda *a: None)   # like FLUX.2: no canny patch
    seen = run_job(monkeypatch, base())
    p = seen["params"]
    assert p["task"] == "edit" and p["image"] == "t.png" and (p["src_w"], p["src_h"]) == (960, 960)
    assert p["use_mask"] is False and p["mask"] is None and p["mode"] == "inpaint" and p["denoise"] == 1.0
    assert seen["run"]["params"]["template"]["image"] == "t.png"
    assert any(n["class_type"] == "LoadImage" and n["inputs"]["image"] == "t.png" for n in seen["graph"].values())


def test_template_conflicts_are_refused(monkeypatch):
    with pytest.raises(HTTPException) as e:
        run_job(monkeypatch, base(control={"type": "canny", "image": "c.png"}))
    assert e.value.status_code == 400 and "guidance" in e.value.detail
    with pytest.raises(HTTPException):
        run_job(monkeypatch, base(template={"image": "t.png", "w": 0, "h": 5}))
    with pytest.raises(HTTPException):
        run_job(monkeypatch, base(template={"w": 5, "h": 5}))
    with pytest.raises(HTTPException):   # a family with neither a control patch nor a template edit
        run_job(monkeypatch, base(preset="qwen_2512", family="nope"))


def test_template_note_names_image_one_per_family():
    p = {"prompt": "a vinyl record", "template": {"image": "t.png", "w": 8, "h": 8}}
    assert graphs.edit_prompt({**p, "family": "qwen21"}).endswith(graphs.TEMPLATE_NOTE.format(main="<image1>"))
    assert "Picture 1 is a flat shape template" in graphs.edit_prompt({**p, "family": "qwen_edit"})
    flux = graphs.ref_labels("flux2", 0)[0].capitalize()   # the encoder's own name for image 1
    assert f"{flux} is a flat shape template" in graphs.edit_prompt({**p, "family": "flux2"})
    assert "template" not in graphs.edit_prompt({"prompt": "x", "family": "qwen21"})
