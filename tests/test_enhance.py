import json

import pytest

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


def test_request_names_the_images_of_a_job_with_references():
    text = lambda **kw: enhance.request("m", "sys", "x", **kw)["messages"][1]["content"]
    assert "<image1> is the image to edit, <image2> is a reference" in text(n_images=2)
    assert "<image2> to <image3> are references" in text(n_images=3)
    assert "<image" not in text(n_images=1) and "<image" not in text()   # a single image gets no tags
    assert "<image1> to <image2>" in text(n_images=2, edit=False) and "<image" not in text(n_images=0, edit=False)


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


def test_ideas_request_schema_and_count():
    b = enhance.ideas_request("m", "covers for A and B", 3, same_style=True)
    ideas = b["response_format"]["json_schema"]["schema"]["properties"]["ideas"]
    assert ideas["minItems"] == ideas["maxItems"] == 3
    assert "exactly 3" in b["messages"][1]["content"] and "same style" in b["messages"][1]["content"]
    auto = enhance.ideas_request("m", "x")["response_format"]["json_schema"]["schema"]["properties"]["ideas"]
    assert auto["maxItems"] == enhance.IDEAS_MAX


def test_ideas_parse_same_style_and_title_fields():
    content = json.dumps({"ideas": [
        {"title": "Late Night Jazz", "subject": "A saxophone.", "setting": "A dark bar", "style": "Oil painting", "camera": "Close-up",
         "title_position": "top", "title_size": "large", "title_font": "Didot Bold"},
        {"title": "Gym", "subject": "Dumbbells", "setting": "A gym", "style": "Neon 3D", "camera": "Wide"}]})
    out = enhance.ideas({"choices": [{"message": {"content": f"<think>x</think>```json\n{content}\n```"}}]},
                        same_style=True, with_title=True)
    assert [i["style"] for i in out] == ["Oil painting", "Oil painting"]
    assert out[0]["prompt"] == "A saxophone. A dark bar. Oil painting. Close-up."
    assert (out[0]["title_position"], out[0]["title_font"], out[1]["title_size"]) == ("top", "Didot Bold", "")
    with pytest.raises(ValueError):
        enhance.ideas({"choices": [{"message": {"content": "no json"}}]})


def test_ideas_request_with_title_fields():
    b = enhance.ideas_request("m", "x", fonts=["Futura Bold", "Didot Bold"])
    item = b["response_format"]["json_schema"]["schema"]["properties"]["ideas"]["items"]
    assert item["properties"]["title_font"]["enum"] == ["Futura Bold", "Didot Bold"]
    assert "title_position" in item["required"] and "Didot Bold" in b["messages"][0]["content"]
    plain = enhance.ideas_request("m", "x")["response_format"]["json_schema"]["schema"]["properties"]["ideas"]["items"]
    assert "title_font" not in plain["properties"]


def test_ideas_request_with_template_shapes():
    shapes = "992 × 992 px: a circle 80 % of the picture in the centre"
    system = enhance.ideas_request("m", "x", template=shapes)["messages"][0]["content"]
    assert shapes in system and "no room, no wall and no horizon" in system
    assert "fixed template" not in enhance.ideas_request("m", "x")["messages"][0]["content"]


def test_loaded_instances():
    listing = {"models": [{"key": "a", "loaded_instances": [{"id": "a"}, {"id": "a:2"}]}, {"key": "b", "loaded_instances": []}]}
    assert enhance.loaded_instances(listing, "a") == ["a", "a:2"] and enhance.loaded_instances(listing, "b") == []


def test_lms_listing_feeds_chat_models():
    items = [{"type": "llm", "modelKey": "qwen3.8", "architecture": "qwen35", "vision": False},
             {"type": "llm", "modelKey": "img", "architecture": "qwen_image"},
             {"type": "embedding", "modelKey": "nomic"}]
    assert enhance.chat_models(enhance.lms_listing(items)) == [{"id": "qwen3.8", "vision": False, "loaded": False}]


def test_fixed_title_position_is_not_the_models_choice():
    b = enhance.ideas_request("m", "x", fonts=["Futura Bold"], position="bottom")
    item = b["response_format"]["json_schema"]["schema"]["properties"]["ideas"]["items"]
    assert item["properties"]["title_position"]["enum"] == ["bottom"] and "always bottom" in b["messages"][0]["content"]
    content = json.dumps({"ideas": [{"title": "A", "subject": "s", "setting": "", "style": "", "camera": "",
                                     "title_position": "top", "title_size": "small", "title_font": "Futura Bold"}]})
    out = enhance.ideas({"choices": [{"message": {"content": content}}]}, with_title=True, position="bottom")
    assert out[0]["title_position"] == "bottom"
    auto = enhance.ideas_request("m", "x", fonts=["Futura Bold"])["response_format"]["json_schema"]["schema"]
    assert auto["properties"]["ideas"]["items"]["properties"]["title_position"]["enum"] == ["top", "center", "bottom"]
