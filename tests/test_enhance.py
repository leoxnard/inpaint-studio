import enhance
import installer
import presets


def _template(sub: bool) -> dict:
    nodes = [{"id": 1, "type": "PrimitiveStringMultiline", "widgets_values": ["# Rewrite the prompt"]},
             {"id": 2, "type": "TextGenerate", "inputs": [{"name": "clip", "link": None},
                                                         {"name": "system_prompt", "link": 7}]}]
    g = {"nodes": nodes, "links": [[7, 1, 0, 2, 1, "STRING"]]}
    return {"nodes": [], "links": [], "definitions": {"subgraphs": [g]}} if sub else g


def test_system_prompt_found_top_level_and_in_subgraph():
    assert enhance.system_prompt(_template(False)) == "# Rewrite the prompt"
    assert enhance.system_prompt(_template(True)) == "# Rewrite the prompt"


def test_system_prompt_missing_raises():
    try:
        enhance.system_prompt({"nodes": [], "links": []})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_chat_models_drops_image_models_and_sorts_loaded_first():
    listing = {"data": [
        {"id": "qwen-image-2.1@q8_0", "type": "llm", "arch": "qwen_image21", "state": "not-loaded"},
        {"id": "nomic-embed", "type": "embeddings", "arch": "nomic-bert", "state": "not-loaded"},
        {"id": "b-text", "type": "llm", "arch": "qwen3", "state": "not-loaded"},
        {"id": "a-vision", "type": "vlm", "arch": "qwen35", "state": "not-loaded"},
        {"id": "c-loaded", "type": "llm", "arch": "llama", "state": "loaded"}]}
    assert enhance.chat_models(listing) == [
        {"id": "c-loaded", "vision": False, "loaded": True},
        {"id": "a-vision", "vision": True, "loaded": False},
        {"id": "b-text", "vision": False, "loaded": False}]


def test_request_text_and_images():
    body = enhance.request("m", "sys", "a dog", seed=7)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["content"].startswith("a dog") and "English" in body["messages"][1]["content"]
    assert body["reasoning_effort"] == "none" and body["seed"] == 7
    body = enhance.request("m", "sys", "make it winter", ["data:a", "data:b"])
    parts = body["messages"][1]["content"]
    assert [p["image_url"]["url"] for p in parts[:2]] == ["data:a", "data:b"] and parts[2]["type"] == "text"


def test_request_keeps_a_chinese_prompt_chinese():
    assert "English" not in enhance.request("m", "sys", "一只狗")["messages"][1]["content"]


def test_wrong_language():
    assert enhance.wrong_language("a dog", "一只狗在草地上")
    assert not enhance.wrong_language("一只狗", "一只狗在草地上")
    assert not enhance.wrong_language("a dog", "A dog on the grass.")


def test_answer_is_cleaned():
    r = {"choices": [{"message": {"content": "<think>hm</think>\n```\nPrompt: \"A dog\nin snow.\"\n```"}}]}
    assert enhance.answer(r) == "A dog in snow."
    assert enhance.answer({}) == ""


def _have(**on):
    have = {f"component:{c}": True for c in presets.COMPONENTS}
    have.update({f"model:{p}:{q}": True for p, pr in presets.PRESETS.items() for q in pr["quants"]})
    have.update(on)
    return have


def test_job_missing_lists_the_preset_files_and_extras():
    params = {"preset": "qwen21_uc", "quant": "Q8_0", **presets.resolve("qwen21_uc", "Q8_0")}
    assert installer.job_missing(_have(), params) == []
    have = _have(**{"model:qwen21_uc:Q8_0": False, "component:qwen3vl_8b": False, "component:up_realesrgan_x2": False})
    assert installer.job_missing(have, params, ["up_realesrgan_x2"]) == [
        "model:qwen21_uc:Q8_0", "component:qwen3vl_8b", "component:up_realesrgan_x2"]


def test_job_missing_ignores_an_advanced_override():
    params = {"preset": "qwen21_uc", "quant": "Q8_0", **presets.resolve("qwen21_uc", "Q8_0"), "unet": "my_own.gguf"}
    assert installer.job_missing(_have(**{"model:qwen21_uc:Q8_0": False}), params) == []


def test_item_info_has_title_and_size():
    assert installer.item_info("component:sam3")["size"] == presets.COMPONENTS["sam3"]["size"]
    m = installer.item_info("model:qwen21_uc:Q8_0")
    assert m["title"].endswith("(Q8_0)") and m["size"] == presets.PRESETS["qwen21_uc"]["quants"]["Q8_0"]["size"]
