import graphs


def test_scale_matches_comfy_rounding():
    # 1182x665 at 1.0 MP is what ComfyUI produced in practice
    assert graphs.scale_to_megapixels(1182, 665, 1.0) == (1376, 768)


def test_known_gray_and_good_sizes():
    bad = graphs.size_report(1182, 665, 1.0, 1024)
    assert not bad["safe"] and bad["target_tokens"] == 4128
    good = graphs.size_report(1182, 665, 0.95, 1008)
    assert good["safe"]


def test_square_1024_is_unsafe():
    assert not graphs.size_report(1024, 1024, 1.0, 1024)["safe"]


def test_safe_settings_stays_below_limit():
    s = graphs.safe_settings(1182, 665, 1.0, 1024)
    assert s["target_tokens"] < graphs.TOKEN_LIMIT and s["ref_tokens"] < graphs.TOKEN_LIMIT
    assert s["megapixels"] <= 1.0


def test_edit_graph_with_mask_has_composite():
    p = dict(image="a.png", mask="m.png", use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
             steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=12,
             unet="qwen-image-2.1-UC-Q4_K_M.gguf", clip="c", vae="v", work_w=1312, work_h=736)
    g = graphs.build_edit_graph(p)
    assert g["unet"]["class_type"] == "UnetLoaderGGUF"
    assert g["sampler"]["inputs"]["latent_image"] == ["latent", 0]
    assert "composite" in g and "feather" in g


def test_edit_graph_without_mask_uses_encoder_latent():
    p = dict(image="a.png", mask=None, use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
             steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple",
             unet="x.safetensors", clip="c", vae="v", work_w=1312, work_h=736)
    g = graphs.build_edit_graph(p)
    assert g["sampler"]["inputs"]["latent_image"] == ["encode", 2]
    assert "composite" not in g and g["unet"]["class_type"] == "UNETLoader"


def test_paste_mode_edits_freely_and_composites():
    p = dict(image="a.png", mask="m.png", use_mask=True, mode="paste", megapixels=0.95, resolution=1008, prompt="x",
             steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
             unet="u.gguf", clip="c", vae="v", work_w=1344, work_h=736)
    g = graphs.build_edit_graph(p)
    assert g["sampler"]["inputs"]["latent_image"] == ["latent_src", 0]
    assert "latent" not in g and "out_raw" in g
    assert graphs.KEEP_IDENTICAL in g["encode"]["inputs"]["prompt"]


def test_chunks_cover_all_steps():
    assert graphs.step_chunks(20, 6) == [(0, 6), (6, 12), (12, 18), (18, 20)]


def test_chunked_graph_saves_every_n_steps():
    p = dict(image="a.png", mask="m.png", use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
             steps=6, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
             unet="u.gguf", clip="c", vae="v", work_w=1344, work_h=736, save_every=2, prefix="P")
    g = graphs.build_edit_graph(p)
    assert [k for k in g if k.startswith("stepsave_")] == ["stepsave_2", "stepsave_4", "stepsave_6"]
    assert g["chunk_0"]["inputs"]["noise"] == ["noise", 0] and g["chunk_1"]["inputs"]["noise"] == ["no_noise", 0]
    assert g["chunk_1"]["inputs"]["latent_image"] == ["chunk_fix_0", 0]
    assert g["chunk_fix_0"]["inputs"]["source"] == ["latent", 0]
    assert g["decode"]["inputs"]["samples"] == ["chunk_fix_2", 0] and "sampler" not in g


def test_keep_identical_only_in_paste_mode():
    assert graphs.edit_prompt({"prompt": "x", "mode": "inpaint"}) == "x"
    assert graphs.edit_prompt({"prompt": "x", "mode": "paste", "keep_identical": False}) == "x"


def test_paste_mode_saves_raw_steps_inpaint_does_not():
    base = dict(image="a.png", mask="m.png", use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
                steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
                unet="u.gguf", clip="c", vae="v", work_w=1344, work_h=736, save_every=2, prefix="P")
    paste = graphs.build_edit_graph(dict(base, mode="paste"))
    inpaint = graphs.build_edit_graph(dict(base, mode="inpaint"))
    assert [k for k in paste if k.startswith("stepraw_")] == ["stepraw_2", "stepraw_4"]
    assert not any(k.startswith("stepraw_") for k in inpaint)


def test_save_last_n_steps():
    assert graphs.step_chunks(20, 0, 3) == [(0, 17), (17, 18), (18, 19), (19, 20)]
    assert graphs.step_chunks(10, 4, 2) == [(0, 4), (4, 8), (8, 9), (9, 10)]


def test_output_names_without_mask_png():
    p = dict(image="a.png", mask="m.png", use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
             steps=20, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
             unet="u.gguf", clip="c", vae="v", work_w=1344, work_h=736, save_last=3, prefix="P", mode="paste")
    g = graphs.build_edit_graph(p)
    assert g["out_result"]["inputs"]["filename_prefix"] == "P"
    assert g["out_raw"]["inputs"]["filename_prefix"] == "P_raw"
    assert g["stepsave_20"]["inputs"]["filename_prefix"] == "P/step_20_20"
    assert g["stepraw_17"]["inputs"]["filename_prefix"] == "P/raw_step_17_20"
    assert not any(n["class_type"] == "SaveImage" and "mask" in n["inputs"]["filename_prefix"] for n in g.values())


def test_drop_counter_renames_in_output_dir(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "COMFY_OUTPUT", tmp_path)
    (tmp_path / "InpaintStudio/r1").mkdir(parents=True)
    (tmp_path / "InpaintStudio/r1/step_17_20_00001_.png").write_bytes(b"x")
    img = server.drop_counter({"filename": "step_17_20_00001_.png", "subfolder": "InpaintStudio/r1", "type": "output"})
    assert img["filename"] == "step_17_20.png" and (tmp_path / "InpaintStudio/r1/step_17_20.png").is_file()
    missing = {"filename": "x_00001_.png", "subfolder": "", "type": "output"}
    assert server.drop_counter(missing) == missing
