"""Offline tests: validation, verdicts and the full run flow with a local fake sandbox.

The fake sandbox runs real pytest in a temp folder, so these check the logic,
not the live Daytona/Nosana integration.
"""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import patchpilot_core as core

SOURCES = core.load_fixture()
GOOD_EDIT = {"path": "pricing.py", "old": "return price * percent / 100",
             "new": "return price * (1 - percent / 100)"}
BAD_FIX = {"path": "pricing.py", "old": "return price * percent / 100",
           "new": "return price - percent"}


def proposal(edit):
    return json.dumps({"diagnosis": "d", "explanation": "e", "edits": [edit]})


class FakeSandbox:
    def __init__(self, root):
        self.root, self.id, self.deleted = root, "fake-sandbox", False
        self.fs = self.process = self

    def upload_file(self, data, path):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exec(self, cmd, cwd=None, timeout=None):
        parts = cmd.split(" ", 1)
        if parts[0] in ("python", "python3"):
            cmd = f'"{sys.executable}" {parts[1]}'
        r = subprocess.run(cmd, shell=True, cwd=self.root / (cwd or ""),
                           capture_output=True, text=True, timeout=timeout)
        return SimpleNamespace(exit_code=r.returncode, result=r.stdout + r.stderr)

    def delete(self):
        self.deleted = True


class FakeDaytona:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def create(self):
        return self.sandbox


def fake_llm(content=None, error=None):
    def create(**kwargs):
        if error:
            raise error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def run_with(tmp_path, llm, sources=None, monkeypatch=None):
    if sources is not None:
        monkeypatch.setattr(core, "load_fixture", lambda: sources)
    sandbox = FakeSandbox(tmp_path)
    return core.run_repair("test-model", daytona=FakeDaytona(sandbox), llm=llm), sandbox


# ---- validation ----

def test_valid_edit_accepted():
    path, new = core.validate_proposal(json.loads(proposal(GOOD_EDIT)), SOURCES)
    assert path == "pricing.py" and "1 - percent" in new


@pytest.mark.parametrize("edit, reason", [
    ({**GOOD_EDIT, "path": "test_pricing.py"}, "not allowed"),
    ({**GOOD_EDIT, "path": "../pricing.py"}, "not allowed"),
    ({**GOOD_EDIT, "old": ""}, "empty"),
    ({**GOOD_EDIT, "old": "not in the file"}, "matches 0 times"),
    ({**GOOD_EDIT, "new": GOOD_EDIT["old"]}, "changes nothing"),
])
def test_bad_edits_rejected(edit, reason):
    with pytest.raises(core.EditRejected, match=reason):
        core.validate_proposal(json.loads(proposal(edit)), SOURCES)


def test_two_edits_rejected():
    data = {"diagnosis": "d", "explanation": "e", "edits": [GOOD_EDIT, GOOD_EDIT]}
    with pytest.raises(core.EditRejected, match="exactly one"):
        core.validate_proposal(data, SOURCES)


def test_extract_json_handles_fences():
    assert core.extract_json("```json\n" + proposal(GOOD_EDIT) + "\n```")["diagnosis"] == "d"


def test_extract_json_rejects_prose():
    with pytest.raises(core.EditRejected):
        core.extract_json("Sorry, I cannot help with that.")


def test_verdict_mapping():
    assert core.baseline_verdict(0) == core.NO_FAILURE
    assert core.baseline_verdict(1) is None
    assert core.baseline_verdict(2) == core.ERROR
    assert core.rerun_verdict(0) == core.VERIFIED
    assert core.rerun_verdict(1) == core.NOT_VERIFIED
    assert core.rerun_verdict(4) == core.ERROR


# ---- full flow (fake sandbox, real pytest) ----

def test_genuine_repair_is_verified(tmp_path):
    run, sandbox = run_with(tmp_path, fake_llm(proposal(GOOD_EDIT)))
    assert (run.baseline_exit, run.rerun_exit, run.verdict) == (1, 0, core.VERIFIED)
    assert "+    return price * (1 - percent / 100)" in run.diff
    assert sandbox.deleted


def test_non_fixing_patch_is_not_verified(tmp_path):
    run, sandbox = run_with(tmp_path, fake_llm(proposal(BAD_FIX)))
    assert (run.rerun_exit, run.verdict) == (1, core.NOT_VERIFIED)
    assert sandbox.deleted


def test_out_of_scope_edit_is_error_and_not_applied(tmp_path):
    run, sandbox = run_with(tmp_path, fake_llm(proposal({**GOOD_EDIT, "path": "test_pricing.py"})))
    assert run.verdict == core.ERROR and "not allowed" in run.error
    assert run.rerun_exit is None and run.diff == "" and sandbox.deleted


def test_provider_failure_is_error_with_cleanup(tmp_path):
    run, sandbox = run_with(tmp_path, fake_llm(error=RuntimeError("402 insufficient_credits")))
    assert run.verdict == core.ERROR and "402" in run.error and sandbox.deleted


def test_passing_baseline_stops_without_model_call(tmp_path, monkeypatch):
    fixed = {**SOURCES, "pricing.py": SOURCES["pricing.py"].replace(GOOD_EDIT["old"], GOOD_EDIT["new"])}
    run, sandbox = run_with(tmp_path, fake_llm(error=AssertionError("model must not be called")),
                            sources=fixed, monkeypatch=monkeypatch)
    assert run.verdict == core.NO_FAILURE and run.raw_model_output == "" and sandbox.deleted


def test_local_fixture_is_never_modified(tmp_path):
    before = core.load_fixture()
    run_with(tmp_path, fake_llm(proposal(GOOD_EDIT)))
    assert core.load_fixture() == before
