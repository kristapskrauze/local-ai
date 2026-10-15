"""The rehearsal writes into the repo for the length of the checks and always
puts it back; these cases use throwaway file names and clean up after
themselves.

    python tests/propose_rehearsal_cases.py
"""
import json
import os
import sys
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import propose_patch as P  # noqa: E402
from app import self_heal  # noqa: E402

GREEN = ('def test_ok():\n    assert 1 + 1 == 2\n\n\n'
         'if __name__ == "__main__":\n    test_ok()\n    print("OK")\n')
RED = ('def test_bad():\n    assert 1 + 1 == 3, "arithmetic broke"\n\n\n'
       'if __name__ == "__main__":\n    test_bad()\n    print("OK")\n')


def _name():
    return "tests/zz_rehearsal_%s_cases.py" % uuid.uuid4().hex[:8]


def _gone(path):
    return not os.path.exists(os.path.join(ROOT, *path.split("/")))


def test_a_green_rehearsal_leaves_nothing_behind():
    path = _name()
    res = self_heal.apply_edits([{"path": path, "find": "", "replace": GREEN}],
                                suites=[path], rehearse=True)
    assert res["green"] is True and res["applied"] is False, res
    assert res["rehearsed"] is True, res
    assert _gone(path), path


def test_a_red_rehearsal_reports_why_and_leaves_nothing_behind():
    path = _name()
    res = self_heal.apply_edits([{"path": path, "find": "", "replace": RED}],
                                suites=[path], rehearse=True)
    assert res["green"] is False and res["applied"] is False, res
    assert "arithmetic broke" in json.dumps(res["suites"], ensure_ascii=False), res
    assert _gone(path), path


def test_an_edit_to_an_existing_file_is_put_back_exactly():
    path = _name()
    real = os.path.join(ROOT, *path.split("/"))
    with open(real, "w", encoding="utf-8", newline="") as fh:
        fh.write(GREEN)
    try:
        res = self_heal.apply_edits(
            [{"path": path, "find": "assert 1 + 1 == 2", "replace": "assert 2 + 2 == 4"}],
            suites=[path], rehearse=True)
        assert res["green"] is True, res
        with open(real, encoding="utf-8", newline="") as fh:
            assert fh.read() == GREEN
    finally:
        os.remove(real)


def _proposal_json(path, body):
    return "```json\n%s\n```" % json.dumps({
        "files": [{"path": path, "why": "probe"}],
        "test": {"suite": path, "case": "test", "asserts": "it passes"},
        "edits": [{"path": path, "find": "", "replace": body}],
        "risks": ["none"], "plan": [path]})


def test_propose_feeds_a_failed_rehearsal_back_and_shows_only_what_passes():
    path = _name()
    replies = [_proposal_json(path, RED), _proposal_json(path, GREEN)]
    seen = []

    def chat(cfg, convo, options=None, **_kw):
        seen.append([dict(m) for m in convo])
        return replies[min(len(seen) - 1, len(replies) - 1)]

    res = P.propose("make the probe pass", {}, allowed=True, chat_fn=chat,
                    evidence={}, rehearse=True)
    assert res["state"] == "PROPOSED", res
    assert res["proposal"].get("rehearsal") == "passed", res["proposal"]
    assert res["proposal"]["edits"][0]["replace"] == GREEN
    assert len(seen) == 2, len(seen)
    assert "arithmetic broke" in json.dumps(seen[1], ensure_ascii=False)
    assert _gone(path), path


def test_propose_reports_the_failure_when_nothing_passes():
    path = _name()

    def chat(cfg, convo, options=None, **_kw):
        return _proposal_json(path, RED)

    res = P.propose("make the probe pass", {}, allowed=True, chat_fn=chat,
                    evidence={}, rehearse=True)
    assert res["state"] == "INCOMPLETE" and res["proposal"] is None, res
    assert "arithmetic broke" in str(res.get("reason")), res.get("reason")
    assert _gone(path), path


def test_the_summary_he_approves_says_it_was_tried():
    path = _name()

    def chat(cfg, convo, options=None, **_kw):
        return _proposal_json(path, GREEN)

    res = P.propose("make the probe pass", {}, allowed=True, chat_fn=chat,
                    evidence={}, rehearse=True)
    assert "izmēģināt" in P.render(res).lower(), P.render(res)
    assert _gone(path), path


def test_the_propose_tool_tells_the_model_whether_it_was_tried():
    from unittest.mock import patch
    from app import agent_tools as T
    from app import messaging as M
    from app import rig_agent
    proposal = {"files": [{"path": "app/x.py", "why": "w"}],
                "test": {"suite": "tests/x_cases.py", "case": "c", "asserts": "a"},
                "edits": [], "patch": "", "risks": ["r"],
                "plan": ["tests/x_cases.py"], "rehearsal": "passed"}
    ctx = T.AgentContext(conversation_id="zz-rehearsal-probe", sender="owner")

    def tool_says(prop):
        canned = {"ok": True, "state": "PROPOSED", "proposal": prop}
        with patch.object(M, "load_config", return_value={"auto_reply": {}}), \
                patch.object(rig_agent, "owner_allowed", return_value=True), \
                patch.object(P, "propose", return_value=canned), \
                patch.object(M, "remember_last_proposal"):
            return T._tool_antons_propose_fix({"goal": "probe"}, ctx)

    tried = tool_says(dict(proposal))
    assert str(tried.get("rehearsal", "")).startswith("passed"), tried
    untried = tool_says({k: v for k, v in proposal.items() if k != "rehearsal"})
    assert str(untried.get("rehearsal", "")).startswith("not tried"), untried


def test_a_made_up_name_is_answered_with_the_real_ones():
    note = ('pyright failed on the changed files, so they were put back: '
            'tests/x_cases.py:10 "_build_registry" is not a known attribute of '
            'module "app.agent_tools"')
    text = P._rehearsal_failure({"green": False, "note": note, "suites": []})
    assert "def build_default_registry(" in text, text
    crash = {"green": False, "note": "the suites did not pass", "suites": [
        {"suite": "tests/x_cases.py", "ok": False,
         "output": "ImportError: cannot import name 'propose_it' from 'app.propose_patch'"}]}
    assert "def propose(" in P._rehearsal_failure(crash)
    # then Tool.timeout, three more times -- the field is timeout_seconds
    attr = ('pyright failed on the changed files, so they were put back: '
            'tests/x_cases.py:13 Cannot access attribute "timeout" for class "Tool"')
    text = P._rehearsal_failure({"green": False, "note": attr, "suites": []})
    assert "timeout_seconds" in text, text
    local = ("ruff failed on the changed files, so they were put back: "
             "tests/propose_rehearsal_cases.py:9 Undefined name "
             "`test_a_made_up_name_is_answerd_with_real_ones`")
    text = P._rehearsal_failure({"green": False, "note": local, "suites": []})
    assert "def test_a_made_up_name_is_answered_with_the_real_ones(" in text, text


def test_a_failed_rehearsal_lets_the_goal_change_what_a_test_pins():
    crash = {"green": False, "note": "rehearsal: the suites did not pass", "suites": [
        {"suite": "tests/x_cases.py", "ok": False,
         "output": "AssertionError: 'Lūdzis: Anna' not in ''"}]}
    text = P._rehearsal_failure(crash)
    assert "Lūdzis: Anna" in text, text
    assert "everything the GOAL does not ask to change" in text, text
    assert "update that assertion to the new behaviour" in text, text
    assert "change the code, not the test" not in text, text


def test_the_proposal_prompt_names_functions_that_exist():
    import re
    src = open(os.path.join(ROOT, "app", "agent_tools.py"), encoding="utf-8").read()
    named = re.findall(r"`(\w+)` in app/agent_tools\.py", P._SYS)
    assert named, P._SYS
    for name in named:
        assert "def %s(" % name in src, name


if __name__ == "__main__":
    test_a_made_up_name_is_answered_with_the_real_ones()
    test_a_failed_rehearsal_lets_the_goal_change_what_a_test_pins()
    test_the_proposal_prompt_names_functions_that_exist()
    test_a_green_rehearsal_leaves_nothing_behind()
    test_a_red_rehearsal_reports_why_and_leaves_nothing_behind()
    test_an_edit_to_an_existing_file_is_put_back_exactly()
    test_propose_feeds_a_failed_rehearsal_back_and_shows_only_what_passes()
    test_propose_reports_the_failure_when_nothing_passes()
    test_the_summary_he_approves_says_it_was_tried()
    test_the_propose_tool_tells_the_model_whether_it_was_tried()
    print("OK")
