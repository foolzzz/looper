"""Translate-route contracts and recorder metadata at their closest boundaries."""

from dataclasses import asdict, replace
import json

import pytest

from looper.agents.llmrec import LlmrecRoute, ROUTE_FORMAT, RouteFile
from looper.agents.llmrec.capture import Capture, canonical, tokens


def route():
    return RouteFile(id='a' * 22, agent_id='gateway-test', upstream='https://upstream.invalid/v1',
                     created_at='2026-11-05T00:00:00+00:00')


def read(tmp_path, data):
    path = tmp_path / 'route.json'
    path.write_text(json.dumps(data))
    return RouteFile.read(path)


def test_record_defaults_and_translate_format():
    from looper.agents.llmrec import RouteKind

    assert ROUTE_FORMAT == 4
    assert route().kind == RouteKind.RECORD and route().record is True
    attached = LlmrecRoute('a' * 22, 'http://localhost/r/a', 'https://upstream.invalid')
    assert attached.kind is RouteKind.RECORD and attached.record is True


@pytest.mark.parametrize('dialect', ['generic', 'openai', 'deepseek', 'moonshot', 'zhipu', 'qwen', 'vllm',
                                     'minimax', 'ark', 'openrouter'])
@pytest.mark.parametrize('record', [False, True])
def test_translate_route_roundtrip(tmp_path, dialect, record):
    value = replace(route(), kind='translate', record=record, api='openai-chat', dialect=dialect,
                    models={'client': 'upstream'})
    value.write(tmp_path / 'route.json')
    assert RouteFile.read(tmp_path / 'route.json') == value


@pytest.mark.parametrize('changes', [
    {'kind': 'unknown'}, {'kind': []}, {'record': 1}, {'record': None},
    {'api': None}, {'api': 'openai-responses'}, {'dialect': None}, {'dialect': 'unknown'},
    {'dialect': []}, {'models': None}, {'models': {}}, {'models': []},
    {'models': {'': 'upstream'}}, {'models': {'client': ''}}, {'models': {'client': 1}},
    {'models': {'client': 'upstream', 'other': None}},
])
def test_translate_route_rejects_invalid_contract(tmp_path, changes):
    data = asdict(route()) | {'kind': 'translate', 'api': 'openai-chat', 'dialect': 'generic',
                              'models': {'client': 'upstream'}} | changes
    assert read(tmp_path, data) is None


@pytest.mark.parametrize('field,value', [('api', 'openai-chat'), ('dialect', 'generic'),
                                        ('models', {'client': 'upstream'})])
def test_record_route_rejects_translate_options(tmp_path, field, value):
    assert read(tmp_path, asdict(route()) | {field: value}) is None


def test_translation_metadata_counts_toward_buffer_size():
    item = Capture()
    before = item.size()
    item.translation = {'api': 'openai-chat', 'upstream_usage': {'prompt_tokens': 12}}
    assert item.size() - before == len(canonical(item.translation)) - len(canonical(None))


@pytest.mark.parametrize('details,expected', [({}, 0), ({'cache_write_tokens': 7}, 7),
    ({'cache_write_tokens': True}, None), ({'cache_write_tokens': -1}, None)])
def test_chat_capture_reads_cache_write_tokens(details, expected):
    usage = {'prompt_tokens': 20, 'completion_tokens': 3, 'prompt_tokens_details': details}
    assert tokens('/v1/chat/completions', {'usage': usage})[3] == expected
