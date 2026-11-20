"""The completion_failed dialog's message check (``evals/prompt_regression/dialogs.py``) on synthetic text."""

from __future__ import annotations

from evals.prompt_regression import dialogs

TASK = "task-ab12cd34ef56"
LINK = f"[https://x.test/#tasks/{TASK}](https://x.test/#tasks/{TASK})"


def problem(words: str, *, fake: bool = False, link: str = LINK) -> str | None:
    return dialogs.completion_message_problem(f"{words}\n\n{link}", TASK, fake=fake)


def test_a_good_message_passes():
    assert problem("任务失败了：花费到了 $0.01 的上限。\n要我提高上限后重试吗？") is None


def test_the_code_notice_fails():
    assert "code notice" in dialogs.completion_message_problem(f"任务「x」失败：上限\n{LINK}", TASK, fake=False)


def test_the_link_must_name_the_task():
    assert "no link" in problem("花费上限到了。", link="[l](https://x.test/#tasks/task-other)")


def test_it_must_say_why():
    assert "spend cap" in problem("任务没做完。")


def test_a_retry_is_only_a_question():
    assert "not as a question" in problem("花费到了上限，我会重试。")
    assert problem("花费到了上限。要我重试吗？") is None
    assert problem("Budget cap reached. Want me to retry?") is None


def test_at_most_eight_lines_of_words():
    nine = "\n".join(f"预算第{i}行" for i in range(9))
    assert "9 lines" in problem(nine)
    assert problem("\n".join(f"预算第{i}行" for i in range(8))) is None


def test_fake_mode_checks_only_the_path():
    assert problem('{"status": "ok"}', fake=True) is None
    assert "no link" in problem("x", fake=True, link="")


def test_the_dialog_is_registered_with_the_seed():
    dialog = next(d for d in dialogs.DIALOGS if d.id == "completion_failed")
    assert dialog.seed is dialogs._seed_completion_failed and len(dialog.says) == 1
    assert [call[0] for call in dialog.says[0].fake] == ["looper_task_create", "looper_task_control"]
