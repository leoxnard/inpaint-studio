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


def test_empty_sampler_and_scheduler_fall_back():
    p = dict(image="a.png", mask="m.png", use_mask=True, megapixels=0.95, resolution=1008, prompt="x",
             steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="", scheduler="", feather=12,
             unet="qwen-image-2.1-UC-Q4_K_M.gguf", clip="c", vae="v", work_w=1312, work_h=736)
    inputs = graphs.build_edit_graph(p)["sampler"]["inputs"]
    assert (inputs["sampler_name"], inputs["scheduler"]) == ("euler", "simple")


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
    again = server.drop_counter({"filename": "step_17_20_00001_.png", "subfolder": "InpaintStudio/r1", "type": "output"})
    assert again["filename"] == "step_17_20.png"   # renamed by another server already
    missing = {"filename": "x_00001_.png", "subfolder": "", "type": "output"}
    assert server.drop_counter(missing) == missing


def test_matching_resolution_gives_reference_of_working_size():
    for w, h in [(1344, 736), (1024, 1024), (832, 1216), (1184, 864)]:
        r = graphs.matching_resolution(w, h)
        assert graphs.reference_size(w, h, r) == (w, h), (w, h, r, graphs.reference_size(w, h, r))
    assert graphs.matching_resolution(1344, 736) == 992


# ------------------------------------------------------------------ extra reference images
def _ref_graph(family, task="edit", refs=("a_ref.png", "b_ref.png")):
    p = dict(image="img.png", mask=None, use_mask=False, megapixels=0.95, resolution=1008, prompt="x",
             negative="", steps=8, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
             work_w=1024, work_h=1024, prefix="P", unet="u.gguf", clip="c.safetensors", vae="v.safetensors",
             family=family, task=task, refs=list(refs))
    return graphs.build_edit_graph(p)


def test_qwen21_edit_refs_follow_the_edited_image():
    enc = _ref_graph("qwen21")["encode"]["inputs"]
    assert enc["images.image_1"] == ["scale", 0]
    assert enc["images.image_2"] == ["ref1_scale", 0] and enc["images.image_3"] == ["ref2_scale", 0]


def test_qwen21_generate_refs_start_at_image_1():
    g = _ref_graph("qwen21_turbo", task="generate")
    enc = g["encode"]["inputs"]
    assert enc["images.image_1"] == ["ref1_scale", 0] and "images.image_3" not in enc
    assert g["ref1_load"]["inputs"]["image"] == "a_ref.png" and "load" not in g


def test_qwen_edit_refs_use_image2_and_image3_only():
    g = _ref_graph("qwen_edit", refs=("1.png", "2.png", "3.png"))
    enc = g["encode_raw"]["inputs"]
    assert enc["image2"] == ["ref1_scale", 0] and enc["image3"] == ["ref2_scale", 0] and "image4" not in enc
    assert "ref3_load" not in g


def test_families_without_reference_slots_ignore_refs():
    g = _ref_graph("zimage")
    assert not any(k.startswith("ref") for k in g)


def test_no_refs_keeps_the_single_image_graph():
    enc = _ref_graph("qwen21", refs=())["encode"]["inputs"]
    assert [k for k in enc if k.startswith("images.")] == ["images.image_1"]


def test_edit_with_refs_gets_the_hidden_instruction():
    enc = _ref_graph("qwen21")["encode"]["inputs"]["prompt"]
    assert enc.startswith("<image1> is the image to edit.") and "Take from <image2> and <image3> only what the instruction asks for." in enc
    assert enc.endswith("x")


def test_hidden_instruction_uses_picture_for_edit_2511_and_skips_generate_and_no_refs():
    assert _ref_graph("qwen_edit", refs=("a.png",))["encode_raw"]["inputs"]["prompt"].startswith("Picture 1 is the image to edit.")
    assert _ref_graph("qwen21", task="generate")["encode"]["inputs"]["prompt"] == "x"
    assert _ref_graph("qwen21", refs=())["encode"]["inputs"]["prompt"] == "x"


def test_reference_takes_become_replacement_orders_after_the_prompt():
    p = dict(prompt="remove the tshirt", family="qwen21", task="edit", refs=["a.png", "b.png"],
             ref_takes=["", "face. "])
    enc = graphs.edit_prompt(p)
    assert "remove the tshirt\n\nReplace the face in <image1> with the face from <image3>, " \
           "in the place and at the size of the face in <image1>. Replace only this part itself:" in enc
    assert enc.endswith("stays unless the instruction changes it.")
    assert "<image2> take" not in enc and "Replace the  in" not in enc
    assert graphs.edit_prompt({**p, "ref_note": ""}).startswith("remove the tshirt\n\nReplace the face in <image1>")
    assert "with the face from Picture 3, in the place" in graphs.edit_prompt({**p, "family": "qwen_edit"})
    assert graphs.edit_prompt({**p, "task": "generate"}) == "remove the tshirt"


def test_custom_reference_note_replaces_or_turns_off_the_default():
    p = dict(prompt="x", family="qwen21", task="edit", refs=["a.png"])
    assert graphs.edit_prompt({**p, "ref_note": "  Keep image 1.  "}) == "Keep image 1.\n\nx"
    assert graphs.edit_prompt({**p, "ref_note": ""}) == "x"


def test_custom_keep_note_replaces_or_turns_off_the_paste_instruction():
    p = {"prompt": "x", "mode": "paste"}
    assert graphs.edit_prompt({**p, "keep_note": " Same picture. "}) == "x\n\nSame picture."
    assert graphs.edit_prompt({**p, "keep_note": ""}) == "x"
    assert graphs.edit_prompt({"prompt": "x", "mode": "inpaint", "keep_note": "Same picture."}) == "x"


def test_reference_crop_cuts_the_part_and_does_not_scale_it_up():
    g = _ref_graph("qwen21")
    assert "ref1_crop" not in g
    p = dict(image="img.png", mask=None, use_mask=False, megapixels=0.95, resolution=1008, prompt="x",
             negative="", steps=8, denoise=1.0, seed=1, cfg=1.0, sampler="euler", scheduler="simple", feather=0,
             work_w=1024, work_h=1024, prefix="P", unet="u.gguf", clip="c.safetensors", vae="v.safetensors",
             family="qwen21", task="edit", refs=["a.png", "b.png"],
             ref_crops=[{"x": 10, "y": 20, "w": 300, "h": 400}, None])
    g = graphs.build_edit_graph(p)
    assert g["ref1_crop"]["inputs"] == {"image": ["ref1_load", 0], "width": 300, "height": 400, "x": 10, "y": 20}
    assert g["ref1_scale"]["inputs"]["image"] == ["ref1_crop", 0] and g["ref1_scale"]["inputs"]["megapixels"] == 0.12
    assert "ref2_crop" not in g and g["ref2_scale"]["inputs"]["megapixels"] == 0.95
    assert graphs.crop_box({"x": 0, "y": 0, "w": 8, "h": 100}) is None and graphs.crop_box("x") is None


def test_node_phase_maps_nodes_to_the_workflow_strip():
    chunks = graphs.step_chunks(12, 1)
    assert graphs.node_phase("encode", "TextEncodeQwenImage21", n_refs=2) == {"phase": "encode", "detail": "+ 2 refs"}
    assert graphs.node_phase("chunk_3", "SamplerCustomAdvanced", chunks)["phase"] == "sample"
    assert graphs.node_phase("chunk_dec_3", "VAEDecode", chunks)["detail"] == "step 4"
    assert graphs.node_phase("chunk_dec_11", "VAEDecode", chunks)["detail"] == "result"
    assert graphs.node_phase("stepsave_4", "SaveImage")["phase"] == "save"
    assert graphs.node_phase("ref1_crop", "ImageCrop")["phase"] == "load"
    assert graphs.node_phase("unet", "UnetLoaderGGUF")["phase"] == "load"
    assert graphs.node_phase("upto_1", "SplitSigmas") is None and graphs.node_phase("noise", "RandomNoise") is None


def test_clean_overlays_adds_the_watermark_instruction():
    p = dict(prompt="x", family="qwen21", task="edit", mode="paste", clean_overlays=True)
    enc = graphs.edit_prompt(p)
    assert enc.startswith("x\n\nRemove all watermarks") and enc.endswith(graphs.KEEP_IDENTICAL)
    assert graphs.edit_prompt({**p, "task": "generate", "mode": None}) == "x\n\n" + graphs.CLEAN_NOTE_GENERATE
    assert graphs.edit_prompt({**p, "clean_overlays": False, "mode": None}) == "x"


def test_upscale_graph_fits_the_factor():
    g = graphs.build_upscale_graph("in.png", {"scale": 4}, {"model": "up4.pth"}, 2, "InpaintStudio/x")
    assert g["up_fit"]["inputs"]["scale_by"] == 0.5
    assert g["out_result"]["inputs"]["images"] == ["up_fit", 0]
    g = graphs.build_upscale_graph("in.png", {"scale": 2}, {"model": "up2.pth"}, 2, "InpaintStudio/x")
    assert "up_fit" not in g and g["out_result"]["inputs"]["images"] == ["up", 0]


def test_seedvr2_graph_follows_the_template():
    g = graphs.build_upscale_graph("in.png", {"engine": "seedvr2"}, {"model": "s.safetensors", "vae": "v.safetensors"},
                                   3, "InpaintStudio/x", color_correction="wavelet", seed=7)
    assert g["resize"]["inputs"]["scale_by"] == 3
    assert g["sampler"]["inputs"]["steps"] == 1 and g["sampler"]["inputs"]["seed"] == 7
    assert g["post"]["inputs"]["color_correction_method"] == "wavelet"
    assert g["out_result"]["inputs"]["images"] == ["post", 0]


def test_seedvr2_upscales_the_edit_result_in_the_same_graph():
    import presets
    p = dict(presets.resolve("qwen21_uc", None), family="qwen21", task="edit", image="a.png", mask=None, megapixels=0.95,
             resolution=1008, prompt="x", negative="", steps=4, denoise=1.0, seed=5, cfg=1.0, sampler="euler",
             scheduler="simple", feather=0, work_w=1024, work_h=1024, prefix="P", upscale=2,
             upscale_model="s.safetensors", upscale_engine="seedvr2", upscale_vae="v.safetensors")
    g = graphs.build_edit_graph(p)
    assert g["sv_resize"]["inputs"]["image"] == g["out_result"]["inputs"]["images"]
    assert g["sv_resize"]["inputs"]["scale_by"] == 2 and g["sv_sampler"]["inputs"]["seed"] == 5
    assert g["sampler"]["inputs"]["steps"] == 4   # the edit's own sampler is untouched
    assert g["out_upscaled"]["inputs"] == {"images": ["sv_post", 0], "filename_prefix": "P_x2"}
    assert "up_model" not in g


def test_loras_are_chained_after_the_model_loader():
    import presets
    p = dict(presets.resolve("qwen21_uc", None), family="qwen21", task="edit", image="a.png", mask=None, megapixels=0.95,
             resolution=1008, prompt="x", negative="", steps=4, denoise=1.0, seed=1, cfg=1.0, sampler="euler",
             scheduler="simple", feather=0, work_w=1024, work_h=1024, prefix="P")
    plain = graphs.build_edit_graph(p)
    assert "unet_file" not in plain and plain["unet"]["class_type"].startswith(("Unet", "UNET"))
    g = graphs.build_edit_graph({**p, "loras": [{"name": "a.safetensors", "strength": 0.8},
                                                 {"name": "off.safetensors", "strength": 0},
                                                 {"name": "b.safetensors", "strength": 1.2}]})
    assert g["unet_file"]["class_type"] == plain["unet"]["class_type"]
    assert g["lora_0"]["inputs"] == {"model": ["unet_file", 0], "lora_name": "a.safetensors", "strength_model": 0.8}
    assert g["unet"]["inputs"] == {"model": ["lora_0", 0], "lora_name": "b.safetensors", "strength_model": 1.2}
    users = [n for n, v in g.items() if ["unet", 0] in v["inputs"].values()]
    assert users   # the sampler side still uses "unet", now the last LoRA


def test_outpaint_adds_the_fill_note():
    p = {"prompt": "x", "outpaint": {"canvas_w": 10, "canvas_h": 10, "x": 0, "y": 0}}
    assert graphs.edit_prompt(p).endswith(graphs.OUTPAINT_NOTE)
    assert graphs.OUTPAINT_NOTE not in graphs.edit_prompt({"prompt": "x"})
