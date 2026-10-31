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


class FakeGraph:
    """Records calls instead of talking to Neo4j."""

    def __init__(self, fail_on=None):
        self.calls, self.fail_on, self.closed = [], fail_on, False

    def _call(self, name, *args):
        self.calls.append((name, args))
        if name == self.fail_on:
            raise RuntimeError(f"neo4j {name} broke")

    def record_code(self, g):
        self._call("record_code", g)

    def record_failures(self, run_id, failures):
        self._call("record_failures", run_id, failures)

    def context(self, run_id):
        self._call("context", run_id)
        return core.GRAPH_CONTEXT_QUERY, [{"failing_test": "test_zero_discount",
                                          "function": "apply_discount", "file": "pricing.py",
                                          "past_verified_repairs": 2}]

    def record_repair(self, *args):
        self._call("record_repair", *args)

    def close(self):
        self.closed = True


def run_with(tmp_path, llm, sources=None, monkeypatch=None, graph=None):
    if sources is not None:
        monkeypatch.setattr(core, "load_fixture", lambda: sources)
    sandbox = FakeSandbox(tmp_path)
    return core.run_repair("test-model", daytona=FakeDaytona(sandbox), llm=llm, graph=graph), sandbox


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


def test_embedding_models_are_not_offered():
    ids = ["nvidia/nemotron-3-embed-1b", "qwen/qwen3.8-27b", "some/reranker-rerank"]
    assert core.chat_models(ids) == ["qwen/qwen3.8-27b"]


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


# ---- Neo4j graph context ----

def test_code_graph_from_fixture():
    g = core.build_code_graph(SOURCES)
    assert g["defs"] == [{"file": "pricing.py", "name": "apply_discount"}]
    assert {"src": "test_pricing.py", "dst": "pricing.py"} in g["imports"]
    assert {t["name"] for t in g["tests"]} == {"test_normal_discount", "test_zero_discount",
                                                "test_full_discount"}
    assert all(t["targets"] == ["apply_discount"] for t in g["tests"])


def test_parse_failing_tests():
    out = ("FAILED test_pricing.py::test_zero_discount - assert 0.0 == 100\n"
           "FAILED test_pricing.py::test_full_discount - assert 100.0 == 0\n")
    assert core.parse_failing_tests(out) == [
        {"file": "test_pricing.py", "test": "test_zero_discount"},
        {"file": "test_pricing.py", "test": "test_full_discount"}]


def capturing_llm(content):
    seen = {}

    def create(**kwargs):
        seen["messages"] = kwargs["messages"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), seen


def test_graph_context_reaches_model_and_repair_is_recorded(tmp_path):
    llm, seen = capturing_llm(proposal(GOOD_EDIT))
    graph = FakeGraph()
    run, _ = run_with(tmp_path, llm, graph=graph)
    assert run.verdict == core.VERIFIED
    assert run.graph_rows and "connected" in run.graph_status
    assert "Code graph context from Neo4j" in seen["messages"][1]["content"]
    names = [c[0] for c in graph.calls]
    assert names == ["record_code", "record_failures", "context", "record_repair"]
    failures = graph.calls[1][1][1]
    assert len(failures) == 3  # all three seeded failures reach the graph
    assert graph.closed


@pytest.mark.parametrize("step", ["record_code", "context", "record_repair"])
def test_neo4j_failure_never_breaks_the_repair(tmp_path, step):
    run, sandbox = run_with(tmp_path, fake_llm(proposal(GOOD_EDIT)), graph=FakeGraph(fail_on=step))
    assert run.verdict == core.VERIFIED and sandbox.deleted
    assert ("unavailable" in run.graph_status) or ("saving the repair failed" in run.graph_status)


def test_missing_neo4j_config_is_reported(tmp_path, monkeypatch):
    for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    run, _ = run_with(tmp_path, fake_llm(proposal(GOOD_EDIT)), graph="auto")
    assert run.verdict == core.VERIFIED and "not configured" in run.graph_status
