"""Pricing a run at its provider's prices, and the soft per-run cap Looper keeps (multi-runtime RFC section 8).

Expected figures are worked out by hand from the prices each test states: tokens times US dollars per million
tokens, for uncached input, output, cache reads and cache writes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from looper.agents.profile_registry import CatalogModel, ProviderSpec
from looper.agents.run_usage import Usage, reported_totals, run_usage
from looper.agents.runtimes import ApiProtocol, AuthKind, CostSource, cap_exceeded, price_cost
from looper.agents.runtimes.pricing import RunSpend, model_price, pricer, usage_event


def _provider(*catalog: CatalogModel) -> ProviderSpec:
    return ProviderSpec("prov-x", "x", AuthKind.API_KEY, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                        base_url="https://example.invalid/anthropic", catalog=catalog)


MAIN = CatalogModel("m-main", input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, cache_read_usd_per_mtok=0.2,
                    cache_write_usd_per_mtok=2.5)


def test_a_runs_cost_is_each_kind_of_token_at_its_price() -> None:
    usage = Usage(input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000, cache_creation_tokens=4_000)
    cost = price_cost(usage, _provider(MAIN), "m-main")
    # 1000 * 2 + 2000 * 10 + 30000 * 0.2 + 4000 * 2.5 = 2000 + 20000 + 6000 + 10000 = 38000 per million
    assert cost.source is CostSource.PROVIDER_PRICE
    assert cost.usage == Usage(cost_usd=0.038, input_tokens=1_000, output_tokens=2_000, cache_read_tokens=30_000,
                               cache_creation_tokens=4_000)


def test_a_catalog_price_wins_and_an_unknown_one_comes_from_the_builtin_table() -> None:
    # glm-5.3's built-in prices: 1.40 input, 4.40 output, 0.26 cache read, 1.40 cache write.
    provider = _provider(CatalogModel("glm-5.3[1m]", output_usd_per_mtok=5.0))
    price = model_price(provider, "glm-5.3[1m]")
    assert (price.input_usd_per_mtok, price.output_usd_per_mtok, price.cache_read_usd_per_mtok,
            price.cache_write_usd_per_mtok) == (1.40, 5.0, 0.26, 1.40)
    # The runtime reports the model without its suffix or with a date: the same prices.
    assert model_price(provider, "glm-5.3") == price
    assert model_price(_provider(), "claude-haiku-4-5-20251001") == model_price(_provider(), "claude-haiku-4-5")


@pytest.mark.parametrize(("auth", "model", "priced_as"), [
    # With Claude Code's own login, an alias is the model it runs (its runs report claude-opus-5-5 for opus).
    (AuthKind.OAUTH, "opus", "claude-opus-5-5"),
    (AuthKind.OAUTH, "sonnet[1m]", "claude-sonnet-5-5"),
    (AuthKind.OAUTH, "haiku", "claude-haiku-4-5"),
    # With another provider, its environment may map an alias to a model of its own: unpriced.
    (AuthKind.API_KEY, "sonnet", None),
])
def test_a_claude_code_alias_is_priced_as_its_model_only_with_claude_codes_own_login(
    auth: AuthKind, model: str, priced_as: str | None,
) -> None:
    provider = ProviderSpec("prov-c", "c", auth, frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                            oauth_runtime_type="claude-code" if auth is AuthKind.OAUTH else None)
    price = model_price(provider, model)
    assert price == (model_price(provider, priced_as) if priced_as else model_price(_provider(), "no-such-model"))
    assert price.complete is (priced_as is not None)


@pytest.mark.parametrize("usage", [
    Usage(input_tokens=10, output_tokens=5),
    Usage(input_tokens=10, cache_read_tokens=3),  # a kind the model has no price for
])
def test_a_model_without_the_prices_it_needs_records_tokens_only(usage: Usage) -> None:
    provider = _provider(CatalogModel("half-priced", input_usd_per_mtok=1.0, output_usd_per_mtok=None))
    model = "mystery-1" if usage.output_tokens else "half-priced"
    cost = price_cost(usage, provider, model)
    assert cost.usage.cost_usd is None and cost.usage.input_tokens == 10


def test_a_result_is_priced_model_by_model_and_a_resumed_turn_counts_only_its_own_share() -> None:
    provider = _provider(MAIN)  # the background model (glm-5.3-flash) is priced by the built-in table

    def result(main_input: int, flash_output: int) -> dict:
        return {"type": "result", "total_cost_usd": 99.0, "modelUsage": {
            "m-main": {"inputTokens": main_input, "outputTokens": 0, "cacheReadInputTokens": 0,
                       "cacheCreationInputTokens": 0},
            "glm-5.3-flash[1m]": {"inputTokens": 0, "outputTokens": flash_output, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0}}}

    price = pricer(provider)
    first = reported_totals(result(1_000_000, 1_000_000), price)
    # Claude Code's 99.0 is ignored: 1M input at 2.0 + 1M output at 0.50 (glm-5.3-flash).
    assert first.cost_usd == pytest.approx(2.5) and first.input_tokens == 1_000_000
    # The next result of the same session reports running totals; the turn's own share is the difference.
    own = run_usage(result(1_500_000, 1_000_000), before=first, price=price)
    assert own.cost_usd == pytest.approx(1.0) and own.input_tokens == 500_000


def test_the_soft_cap_counts_usage_events_at_the_providers_prices() -> None:
    spend = RunSpend(_provider(MAIN), "m-main", cap=0.5)
    request = usage_event("m-main", Usage(input_tokens=200_000, output_tokens=0))  # $0.40
    assert spend.add({"type": "assistant"}) is False
    assert spend.add(request) is False and not spend.exceeded
    assert spend.add(request) is True and spend.exceeded  # $0.80: the request that crossed it
    assert spend.add(request) is False  # already over
    assert spend.usage.cost_usd == pytest.approx(1.2) and spend.usage.input_tokens == 600_000
    # An unpriced request takes the run out of the cap, and nothing without a cap is ever over it.
    unpriced = RunSpend(_provider(MAIN), "m-main", cap=0.0001)
    unpriced.add(usage_event("mystery-1", Usage(input_tokens=1)))
    unpriced.add(request)
    assert unpriced.usage.cost_usd is None and not unpriced.exceeded
    assert (cap_exceeded(1.0, None), cap_exceeded(None, 1.0), cap_exceeded(0.5, 0.5), cap_exceeded(0.6, 0.5)) \
        == (False, False, False, True)


def test_claude_codes_stream_yields_one_usage_event_per_model_request() -> None:
    from looper.agents.claude_cli import stream_events

    def stream(kind: str, parent: str | None = None, **event: object) -> dict:
        return {"type": "stream_event", "parent_tool_use_id": parent, "event": {"type": kind, **event}}

    lines = [
        stream("message_start", message={"model": "m-main", "usage": {"input_tokens": 900, "output_tokens": 0}}),
        {"type": "assistant", "message": {"usage": {"input_tokens": 900, "output_tokens": 0}}},
        stream("message_start", "tool-1", message={"model": "m-sub", "usage": {"input_tokens": 7}}),
        stream("message_delta", usage={"input_tokens": 800, "output_tokens": 30, "cache_read_input_tokens": 100}),
        stream("message_stop"),
        stream("message_delta", "tool-1", usage={"output_tokens": 4}),
        stream("message_stop", "tool-1"),
    ]

    async def _read() -> list[dict]:
        reader = asyncio.StreamReader()
        reader.feed_data("".join(json.dumps(line) + "\n" for line in lines).encode())
        reader.feed_eof()
        return [event async for event in stream_events(reader)]

    usage = [e for e in asyncio.run(_read()) if e["type"] == "usage"]
    assert usage == [
        {"type": "usage", "model": "m-main", "parent_tool_use_id": None,
         "usage": {"input_tokens": 800, "output_tokens": 30, "cache_read_tokens": 100}},
        {"type": "usage", "model": "m-sub", "parent_tool_use_id": "tool-1",
         "usage": {"input_tokens": 7, "output_tokens": 4}},
    ]


@pytest.mark.parametrize("priced", [True, False])
def test_pi_usage_reaches_provider_pricing_and_the_recorders_soft_cap(priced: bool, tmp_path: Path) -> None:
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.claude_cli import ClaudeRunRecorder
    from looper.agents.profile_registry import AgentProfile, RuntimeSpec
    from looper.agents.runtimes.pi import PiRuntime

    # Totals: main (157, 18, 63, 8), nested + compaction (50, 7, 15, 3).
    # At (2, 10, .2, 2.5) and (1, 2, .1, 1): .0005266 + .0000685 = .0005951.
    catalog = (CatalogModel("test-model", input_usd_per_mtok=2, output_usd_per_mtok=10,
                            cache_read_usd_per_mtok=.2, cache_write_usd_per_mtok=2.5),
               CatalogModel("configured-model", input_usd_per_mtok=1, output_usd_per_mtok=2,
                            cache_read_usd_per_mtok=.1, cache_write_usd_per_mtok=1)) if priced else ()
    provider = ProviderSpec("pi-provider", "pi", AuthKind.API_KEY, frozenset({ApiProtocol.OPENAI_RESPONSES}),
                            base_url="https://example.invalid/v1", catalog=catalog)
    profile = AgentProfile("pi-profile", "pi", RuntimeSpec("rt-pi", "pi", "pi"), provider, "configured-model")
    launch = Launch(AgentSettings(profile=profile, max_budget_usd=.0005))
    runtime = PiRuntime()
    recorder = ClaudeRunRecorder(agent_id="worker-test", task_id=None, command=["pi"], cwd=str(tmp_path),
                                 input_text="synthetic", launch=launch)
    crossed: list[int] = []
    results: list[dict] = []

    async def read() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data((Path(__file__).parent / "fixtures/pi_json/compaction.jsonl").read_bytes())
        reader.feed_eof()
        requests = 0
        async for event in runtime.parse(reader, model=launch.model):
            before = recorder.cap_stop
            await recorder.record(event)
            requests += event["type"] == "usage"
            if before is None and recorder.cap_stop is not None:
                crossed.append(requests)
                assert event["type"] == "usage"
            if event["type"] == "result":
                results.append(event)

    asyncio.run(asyncio.wait_for(read(), 5))
    result, = results
    own = run_usage(result, price=launch.price)
    cost = runtime.cost(own, provider)
    assert cost.source is runtime.cost_source(provider) is CostSource.PROVIDER_PRICE
    assert (own.input_tokens, own.output_tokens, own.cache_read_tokens, own.cache_creation_tokens) == (207, 25, 78, 11)
    assert own.cost_usd == (pytest.approx(.0005951) if priced else None)
    if priced:
        assert crossed == [4]  # .0003965 before the recovered request, .0005605 after it.
        stop = recorder.cap_stop
        assert stop is not None and stop.usage == own
        assert runtime.budget_error(stop.result_event)
    else:
        assert crossed == [] and recorder.cap_stop is None
    assert runtime.budget_error(result) is None
    assert runtime.budget_error({"subtype": "error_max_budget_usd"}) is None


@pytest.mark.parametrize("protocol", [ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT])
def test_pi_real_provider_client_separates_cached_input_before_looper_prices_it(
    protocol: ApiProtocol, tmp_path: Path,
) -> None:
    """Use pi's real model loader/client against synthetic SSE via fetch; no operator login or network."""
    from looper.agents.agent_settings import AgentSettings, Launch
    from looper.agents.profile_registry import AgentProfile, RuntimeSpec
    from looper.agents.runtimes.pi import PiStream, _probe_env, models_json

    executable, node = shutil.which("pi"), shutil.which("node")
    if not executable or not node:
        pytest.skip("installed pi 1.3.0 and Node are required for the provider wire usage check")
    package = next((path for path in Path(executable).resolve().parents
                    if (path / "package.json").exists()), None)
    if package is None or json.loads((package / "package.json").read_text()).get("version") != "1.3.0":
        pytest.skip("the provider wire usage check targets pi 1.3.0")
    provider = ProviderSpec("test", "test", AuthKind.API_KEY, frozenset({protocol}),
                            base_url="https://example.invalid/v1", api_key="synthetic-key", catalog=(MAIN,))
    profile = AgentProfile("test", "test", RuntimeSpec("rt-pi", "pi", "pi"), provider, "m-main")
    (tmp_path / "models.json").write_text(json.dumps(models_json(Launch(AgentSettings(profile=profile)))))
    script = tmp_path / "usage.mjs"
    script.write_text('''
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
const [pkg, root] = process.argv.slice(2);
const { ModelRuntime } = await import(pathToFileURL(join(pkg, 'dist/core/model-runtime.js')));
const runtime = await ModelRuntime.create({authPath: join(root, 'auth.json'),
  modelsPath: join(root, 'models.json'), refreshOnCreate: false});
if (runtime.getError()) throw new Error(runtime.getError());
const model = runtime.getModel('looper', 'm-main');
const requests = [];
const fetch = async (url, init) => {
  requests.push({path: new URL(url).pathname, body: JSON.parse(init.body)});
  let data;
  if (model.api === 'openai-responses') {
    const response = {id: 'resp-test', status: 'completed', output: [], usage: {
      input_tokens: 100, input_tokens_details: {cached_tokens: 40, cache_write_tokens: 8},
      output_tokens: 10, output_tokens_details: {reasoning_tokens: 6}, total_tokens: 110}};
    data = 'event: response.completed\\ndata: ' + JSON.stringify({type: 'response.completed', response}) + '\\n\\n';
  } else {
    const chunk = {id: 'chat-test', object: 'chat.completion.chunk', model: 'm-main',
      choices: [{index: 0, delta: {role: 'assistant', content: 'Done.'}, finish_reason: 'stop'}],
      usage: {prompt_tokens: 100, prompt_tokens_details: {cached_tokens: 40, cache_write_tokens: 8},
        completion_tokens: 10, completion_tokens_details: {reasoning_tokens: 6}, total_tokens: 110}};
    data = 'data: ' + JSON.stringify(chunk) + '\\n\\ndata: [DONE]\\n\\n';
  }
  return new Response(data, {headers: {'Content-Type': 'text/event-stream'}});
};
const message = await runtime.complete(model, {
  messages: [{role: 'user', content: 'test', timestamp: 0}],
}, {fetch, signal: AbortSignal.timeout(10000)});
if (message.stopReason !== 'stop') throw new Error(message.errorMessage || message.stopReason);
console.log(JSON.stringify({requests, message}));
''')
    home, agent, session = (tmp_path / name for name in ("home", "agent", "session"))
    for directory in (home, agent, session):
        directory.mkdir()
    env = {**_probe_env(home, agent, session), "LOOPER_PROVIDER_API_KEY": "synthetic-key"}
    done = subprocess.run([node, str(script), str(package), str(tmp_path)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=20, check=False)
    assert done.returncode == 0, done.stderr
    readback = json.loads(done.stdout)
    request, = readback["requests"]
    assert request["path"] == ("/v1/responses" if protocol is ApiProtocol.OPENAI_RESPONSES else "/v1/chat/completions")
    stream = PiStream(model="m-main")
    events = stream.translate({"type": "message_end", "message": readback["message"]})
    result, = stream.translate({"type": "agent_settled", "aborted": False})
    spend = RunSpend(provider, "m-main", cap=.0002)
    assert [spend.add(event) for event in events] == [False, True]
    # (100 - 40 - 8) * 2 + 10 * 10 + 40 * .2 + 8 * 2.5 = 232 per million.
    assert spend.usage == run_usage(result, price=pricer(provider)) == Usage(
        cost_usd=.000232, input_tokens=52, output_tokens=10, cache_read_tokens=40, cache_creation_tokens=8)
