"""Memory dialog scoring checks persisted candidates and the current turn's typed calls."""

from types import SimpleNamespace

import pytest

from evals.prompt_regression import dialogs


def look(actions=(), candidates=(), reply="提议待你确认。"):
    def post(path, body):
        assert path == "/api/v1/memory/list"
        assert body == {"state": "all"}
        return {"result": {"candidates": list(candidates)}}

    return SimpleNamespace(turn_actions=lambda: list(actions), reply=reply, fake=False,
                           api=SimpleNamespace(post=post))


def proposal(kind="preference", *, revise=None):
    return {"tool": "looper_memory_propose", "status": "ok", "arguments": {
        "kind": kind, **({"revise": revise} if revise else {})}, "result": {"candidate": "M-0001"}}


@pytest.mark.parametrize("actions,candidates,passed", [
    ([], [], False),
    ([proposal()], [], False),
    ([proposal()], [{"id": "M-0001", "kind": "preference", "state": "drafting"}], True),
    ([proposal(), proposal()], [{"id": "M-0001", "kind": "preference", "state": "drafting"}], False),
    ([proposal("fact")], [{"id": "M-0001", "kind": "fact", "state": "drafting"}], False),
])
def test_proposal_requires_one_successful_call_and_its_persisted_kind(actions, candidates, passed):
    check = dialogs.select(["memory_preference"])[0].says[0].check
    assert (check(look(actions, candidates)) is None) is passed


@pytest.mark.parametrize("case_id,actions,candidates,passed", [
    ("memory_none", [], [], True),
    ("memory_none", [{**proposal(), "status": "error"}], [], False),
    ("memory_quoted", [proposal("preference")], [], False),
    ("memory_quoted", [proposal("fact")], [], True),
    ("memory_secret", [], [], True),
    ("memory_secret", [], [{"id": "M-0001"}], False),
])
def test_negative_cases_score_the_call_or_candidate_boundary(case_id, actions, candidates, passed):
    check = dialogs.select([case_id])[0].says[0].check
    assert (check(look(actions, candidates)) is None) is passed


@pytest.mark.parametrize("revise,state,passed", [(None, "drafting", False), ("M-1", "pending", False),
                                                ("M-1", "drafting", True)])
def test_revision_requires_the_revise_argument_and_drafting_readback(revise, state, passed):
    check = dialogs.select(["memory_revise"])[0].says[0].check
    assert (check(look([proposal(revise=revise)], [
        {"id": "M-0001", "kind": "preference", "state": state}])) is None) is passed


@pytest.mark.parametrize("reply,passed", [("已记住这个偏好。", False), ("已经保存到知识库。", False),
                                         ("尚未保存，等你确认。", True),
                                         ("提议待你确认。\n（已提议记入记忆 M-0001：已记住，待你确认）", True)])
def test_never_saved_scores_the_model_reply_after_removing_the_footer(reply, passed):
    check = dialogs.select(["memory_never_saved"])[0].says[0].check
    assert (check(look([proposal()], [{"id": "M-0001", "kind": "preference", "state": "drafting"}],
                      reply)) is None) is passed
