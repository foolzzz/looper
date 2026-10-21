"""Provider presets: a known vendor fills a new provider, and the user adds the key.

A context window is copied from an existing preset model of the same name. The
price table has no windows, and a ``[1m]`` suffix is a different name, so those
models keep the preset default (``None``).
"""

from __future__ import annotations

from looper.agents.profile_registry import ModelSpec, _provider_fields
from looper.agents.provider_presets import api_models, presets_view

# The preset defaults; each API's effective catalog is checked separately below.
_EXISTING = (
    ("glm-bigmodel", "GLM (BigModel, China)", "glm",
     ((("anthropic-messages",), "https://open.bigmodel.cn/api/anthropic"),
      (("openai-chat",), "https://open.bigmodel.cn/api/paas/v4"),
      (("openai-responses",), "https://open.bigmodel.cn/api/v1")),
     (("glm-5.3[1m]", 1_000_000, "glm-5.3-flash[1m]"), ("glm-5.3-flash[1m]", 1_000_000, "glm-5.3-flash[1m]"),
      ("glm-5.2[1m]", 1_000_000, "glm-5.3-flash[1m]"), ("glm-4.7", 200_000, "glm-4.5-air")),
     (("API_TIMEOUT_MS", "3000000"), ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),
      ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")),
     ("https://docs.bigmodel.cn/cn/coding-plan/tool/claude", "https://docs.bigmodel.cn/cn/guide/start/quick-start",
      "https://docs.bigmodel.cn/cn/guide/start/model-overview"),
     "BigModel bills in CNY; Looper prices these models at Z.ai's USD list prices unless the catalog says "
     "otherwise"),
    ("glm-zai", "GLM (Z.ai, international)", "glm-zai",
     ((("anthropic-messages",), "https://api.z.ai/api/anthropic"),
      (("openai-chat",), "https://api.z.ai/api/paas/v4"),
      (("openai-responses",), "https://api.z.ai/api/v1")),
     (("glm-5.3[1m]", 1_000_000, "glm-5.3-flash[1m]"), ("glm-5.3-flash[1m]", 1_000_000, "glm-5.3-flash[1m]"),
      ("glm-5.2[1m]", 1_000_000, "glm-5.3-flash[1m]"), ("glm-4.7", 200_000, "glm-4.5-air")),
     (("API_TIMEOUT_MS", "3000000"), ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),
      ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")),
     ("https://docs.z.ai/devpack/tool/claude", "https://docs.z.ai/guides/overview/quick-start",
      "https://docs.z.ai/guides/llm/glm-5.3"),
     ""),
    ("deepseek", "DeepSeek", "deepseek",
     ((("anthropic-messages",), "https://api.deepseek.com/anthropic"),
      (("openai-responses", "openai-chat"), "https://api.deepseek.com")),
     (("deepseek-flash[1m]", 1_000_000, "deepseek-flash"), ("deepseek-v4-pro[1m]", 1_000_000, "deepseek-flash")),
     (("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "786432"),),
     ("https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/",
      "https://api-docs.deepseek.com/guides/anthropic_api",
      "https://api-docs.deepseek.com/guides/responses_api/", "https://api-docs.deepseek.com/"),
     ""),
    ("kimi-moonshot-cn", "Kimi (Moonshot, China)", "kimi",
     ((("anthropic-messages",), "https://api.moonshot.cn/anthropic"),
      (("openai-responses", "openai-chat"), "https://api.moonshot.cn/v1")),
     (("kimi-k3[1m]", 1_000_000, "kimi-k2.7-code"), ("kimi-k2.7-code", 262_144, "kimi-k2.7-code"),
      ("kimi-k2.6", 262_144, "kimi-k2.6")),
     (("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),),
     ("https://platform.kimi.com/docs/guide/claude-code-kimi.md", "https://platform.kimi.com/docs/api/overview"),
     "kimi-k2.7-code always thinks; Moonshot's Claude Code guide uses it as the background model, and asks "
     "for thinking to be on; Moonshot China bills in CNY, and Looper prices these models at the international USD "
     "list prices unless the catalog says otherwise"),
    ("kimi-moonshot", "Kimi (Moonshot, international)", "kimi-intl",
     ((("anthropic-messages",), "https://api.moonshot.ai/anthropic"),
      (("openai-responses", "openai-chat"), "https://api.moonshot.ai/v1")),
     (("kimi-k3[1m]", 1_000_000, "kimi-k2.7-code"), ("kimi-k2.7-code", 262_144, "kimi-k2.7-code"),
      ("kimi-k2.6", 262_144, "kimi-k2.6")),
     (("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),),
     ("https://platform.kimi.ai/docs/guide/claude-code-kimi.md", "https://platform.kimi.ai/docs/api/overview.md"),
     "kimi-k2.7-code always thinks; Moonshot's Claude Code guide uses it as the background model, and asks "
     "for thinking to be on"),
    ("cpa", "CLIProxyAPI on this machine", "cpa",
     ((("openai-responses", "openai-chat"), "http://127.0.0.1:8317/v1"),
      (("anthropic-messages",), "http://127.0.0.1:8317")),
     (("gpt-6.1-sol", 1_050_000, "gpt-6-luna"), ("gpt-5.6-sol", 1_050_000, "gpt-5.6-luna"),
      ("gpt-5.6-luna", 1_050_000, "gpt-5.6-luna")),
     (),
     (),
     "a local proxy for development: its key is one of the api-keys in its config file, and its models are "
     "those it serves"),
)

_OPENCODE_NOTE = (
    "the address may be checked against https://opencode.ai/v2/docs/console/models/; the model list is "
    "https://opencode.ai/inference/v1/models. OpenCode prices some models as ranges, so they are not in the "
    "built-in table; a model whose name is in that table is priced at the vendor list price. Its key is an "
    "OpenCode API key (OPENCODE_API_KEY), sent as Authorization: Bearer"
)
# Each OpenCode API serves one family. A model of another family fails on that endpoint.
_OPENCODE_FAMILIES = (
    (("openai-responses",), "https://opencode.ai/inference/openai/v1",
     (("gpt-6.1-sol", 1_050_000, None), ("gpt-5.6-sol", 1_050_000, None))),
    (("openai-chat",), "https://opencode.ai/inference/openai/v1",
     (("deepseek-v4-pro", None, None), ("glm-5.3", None, None), ("kimi-k3", None, None))),
    (("anthropic-messages",), "https://opencode.ai/inference/anthropic",
     (("claude-opus-5-5", None, None), ("claude-sonnet-5-5", None, None))),
)
_SUB2API_NOTE = (
    "a self-hosted gateway: its key is an sk-... key the gateway issues, sent as Authorization: Bearer, and its "
    "models depend on the accounts behind it, so match this list to /v1/models"
)


def _model_rows(models: list) -> tuple:
    return tuple((model["name"], model["context_window"], model["background_model"]) for model in models)


def _signature(preset: dict) -> tuple:
    return (
        preset["preset_id"], preset["label"], preset["name"],
        tuple((tuple(api["protocols"]), api["base_url"]) for api in preset["apis"]),
        _model_rows(preset["models"]),
        tuple((item["key"], item["value"]) for item in preset["env"]),
        tuple(preset["docs"]), preset["note"],
    )


def _by_id() -> dict[str, dict]:
    views = presets_view()
    assert len(views) == len({p["preset_id"] for p in views})
    return {preset["preset_id"]: preset for preset in views}


def test_existing_preset_defaults_and_api_catalogs() -> None:
    views = presets_view()
    existing = {row[0] for row in _EXISTING}
    assert [_signature(preset) for preset in views if preset["preset_id"] in existing] == [row for row in _EXISTING]
    for preset in views:
        if preset["preset_id"] not in existing:
            continue
        for api in preset["apis"]:
            if preset["preset_id"] == "cpa":
                assert set(api) == {"protocols", "base_url"}
                assert api_models(preset, api) == preset["models"]
                continue
            assert set(api) == {"protocols", "base_url", "models"}
            anthropic = "anthropic-messages" in api["protocols"]
            expected = [{**model, **{field: model[field].removesuffix("[1m]") if model[field] and not anthropic
                                    else model[field] for field in ("name", "background_model")}}
                        for model in preset["models"]]
            assert api["models"] == expected
            assert api_models(preset, api) == expected


def test_opencode_and_sub2api_appear_in_presets_view() -> None:
    views = presets_view()
    assert [preset["preset_id"] for preset in views] == [row[0] for row in _EXISTING] + ["opencode", "sub2api"]
    by_id = _by_id()

    opencode = by_id["opencode"]
    assert _signature(opencode) == (
        "opencode", "OpenCode (Zen)", "opencode",
        tuple((protocols, base_url) for protocols, base_url, _models in _OPENCODE_FAMILIES),
        (("gpt-6.1-sol", 1_050_000, None), ("gpt-5.6-sol", 1_050_000, None), ("claude-opus-5-5", None, None),
         ("claude-sonnet-5-5", None, None), ("deepseek-v4-pro", None, None), ("glm-5.3", None, None),
         ("kimi-k3", None, None)),
        (),
        ("https://opencode.ai/v2/docs/console/models/",),
        _OPENCODE_NOTE,
    )
    assert tuple((tuple(api["protocols"]), api["base_url"], _model_rows(api["models"]))
                 for api in opencode["apis"]) == _OPENCODE_FAMILIES
    for api, (_protocols, _base_url, own) in zip(opencode["apis"], _OPENCODE_FAMILIES, strict=True):
        own_names = {name for name, _window, _background in own}
        other = {name for _p, _u, family in _OPENCODE_FAMILIES if family is not own
                 for name, _window, _background in family}
        assert own_names.isdisjoint(other)

    sub2api = by_id["sub2api"]
    cpa = by_id["cpa"]
    assert _signature(sub2api) == (
        "sub2api", "Sub2API on this machine", "sub2api",
        ((("openai-responses", "openai-chat"), "http://127.0.0.1:8080/v1"),
         (("anthropic-messages",), "http://127.0.0.1:8080")),
        tuple((model["name"], model["context_window"], model["background_model"]) for model in cpa["models"]),
        (),
        ("https://github.com/Wei-Shaw/sub2api",),
        _SUB2API_NOTE,
    )
    assert sub2api["models"] == cpa["models"]
    assert all(set(api) == {"protocols", "base_url"} for api in sub2api["apis"])
    assert all(api_models(sub2api, api) == sub2api["models"] for api in sub2api["apis"])


def test_a_provider_created_from_each_new_preset_api_keeps_its_protocol_and_models() -> None:
    by_id = _by_id()
    for preset_id in ("opencode", "sub2api"):
        preset = by_id[preset_id]
        for index, api in enumerate(preset["apis"]):
            anthropic = "anthropic-messages" in api["protocols"]
            models = api_models(preset, api)
            fields, key = _provider_fields({
                "name": f"{preset['name']}-{index}", "vendor_type": preset_id,
                "endpoints": [{"protocol": protocol, "base_url": api["base_url"]} for protocol in api["protocols"]],
                "env": preset["env"] if anthropic else [], "api_key": "sk-test-preset-key",
            }, None)
            # The models are rows of their own, one per protocol of this API.
            saved = [ModelSpec.from_entry({**model, "id": "draft", "provider_id": "draft", "protocol": protocol})
                     for protocol in api["protocols"] for model in models]
            assert fields["auth"] == "api_key"
            assert [endpoint["protocol"] for endpoint in fields["endpoints"]] == api["protocols"]
            assert all(endpoint["base_url"] == api["base_url"] for endpoint in fields["endpoints"])
            assert [model.name for model in saved] == [model["name"] for model in models] * len(api["protocols"])
            assert key == "sk-test-preset-key"
            if preset_id != "opencode":
                continue
            own = {model["name"] for model in models}
            other = {name for protocols, _url, family in _OPENCODE_FAMILIES
                     if protocols != tuple(api["protocols"]) for name, _window, _background in family}
            assert own.isdisjoint(other)
            assert [endpoint["protocol"] for endpoint in fields["endpoints"]] == list(api["protocols"])
