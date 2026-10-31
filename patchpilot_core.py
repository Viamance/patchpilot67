"""PatchPilot coordinator.

Flow: Daytona baseline -> Nosana proposal -> Daytona verification.
One fixture, one attempt per run, no retry loop. See mvp.md for the spec.
"""

import ast
import difflib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixture"
IMPL_FILE = "pricing.py"
TEST_FILE = "test_pricing.py"
ALLOWED_PATHS = {IMPL_FILE}
REMOTE_DIR = "workspace/fixture"
NOSANA_BASE_URL = "https://inference.nosana.com/v1"

CMD_TIMEOUT = 60       # seconds, each test run
SETUP_TIMEOUT = 180    # seconds, installing pytest if missing
NOSANA_TIMEOUT = 90    # seconds, model request
MAX_OUTPUT = 4000      # characters of pytest output sent to the model

VERIFIED = "VERIFIED"
NOT_VERIFIED = "NOT VERIFIED"
NO_FAILURE = "NO FAILURE REPRODUCED"
ERROR = "ERROR"


class EditRejected(ValueError):
    """The model's proposal failed validation and was not applied."""


@dataclass
class Run:
    model: str | None = None
    stage: str = "idle"
    verdict: str | None = None
    error: str | None = None
    sandbox_id: str | None = None
    cleanup: str | None = None
    test_command: str | None = None
    baseline_exit: int | None = None
    baseline_output: str = ""
    rerun_exit: int | None = None
    rerun_output: str = ""
    diagnosis: str = ""
    explanation: str = ""
    diff: str = ""
    raw_model_output: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    graph_status: str = "not used"
    graph_query: str = ""
    graph_rows: list = field(default_factory=list)
    events: list = field(default_factory=list)
    on_event: object = field(default=None, repr=False)

    def log(self, stage, message):
        self.stage = stage
        self.events.append((time.strftime("%H:%M:%S"), stage, message))
        if self.on_event:
            self.on_event(stage, message)


# ---------- pure helpers (unit-tested) ----------

def load_fixture():
    return {name: (FIXTURE_DIR / name).read_text() for name in (IMPL_FILE, TEST_FILE)}


def baseline_verdict(exit_code):
    """None means a failure was reproduced and the run should continue."""
    if exit_code == 0:
        return NO_FAILURE
    if exit_code == 1:
        return None
    return ERROR


def rerun_verdict(exit_code):
    return {0: VERIFIED, 1: NOT_VERIFIED}.get(exit_code, ERROR)


def extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise EditRejected("model output contains no JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise EditRejected(f"model output is not valid JSON ({e.msg})")


def validate_proposal(data, sources):
    """Return (path, new_source). Raise EditRejected if anything is off."""
    if not isinstance(data, dict):
        raise EditRejected("top-level JSON is not an object")
    for key in ("diagnosis", "explanation"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise EditRejected(f"missing or empty '{key}'")
    edits = data.get("edits")
    if not isinstance(edits, list) or len(edits) != 1:
        raise EditRejected("'edits' must contain exactly one entry")
    edit = edits[0]
    if not isinstance(edit, dict):
        raise EditRejected("edit is not an object")
    path, old, new = edit.get("path"), edit.get("old"), edit.get("new")
    if not all(isinstance(v, str) for v in (path, old, new)):
        raise EditRejected("edit needs string 'path', 'old' and 'new'")
    path = path.strip().removeprefix("./")
    if path not in ALLOWED_PATHS:
        raise EditRejected(f"path '{path}' is not allowed (only {sorted(ALLOWED_PATHS)})")
    if not old.strip():
        raise EditRejected("empty 'old' snippet")
    if old == new:
        raise EditRejected("edit changes nothing")
    count = sources[path].count(old)
    if count != 1:
        raise EditRejected(f"'old' snippet matches {count} times in {path}, expected exactly 1")
    return path, sources[path].replace(old, new, 1)


def make_diff(path, before, after):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))


def build_code_graph(sources):
    """Read our own fixture with Python's ast module: definitions, imports and test targets."""
    defs, tests, imports = [], [], []
    defined = {}
    for path, text in sources.items():
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("test_"):
                defs.append({"file": path, "name": node.name})
                defined[node.name] = path
            if isinstance(node, ast.ImportFrom) and node.module and f"{node.module}.py" in sources:
                imports.append({"src": path, "dst": f"{node.module}.py"})
    for path, text in sources.items():
        for node in ast.parse(text).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                called = {n.func.id for n in ast.walk(node)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                tests.append({"file": path, "name": node.name,
                              "targets": sorted(c for c in called if c in defined)})
    return {"files": sorted(sources), "defs": defs, "tests": tests, "imports": imports}


def parse_failing_tests(output):
    """['test_pricing.py', 'test_x'] pairs from pytest's short summary."""
    seen = []
    for file, test in re.findall(r"FAILED (\S+?\.py)::(\w+)", output):
        if (file, test) not in seen:
            seen.append((file, test))
    return [{"file": f, "test": t} for f, t in seen]


GRAPH_CONTEXT_QUERY = """MATCH (x:Failure {run_id: $run_id})-[:OCCURS_IN]->(t:Test)
      -[:TARGETS]->(fn:Function)<-[:DEFINES]-(file:File)
OPTIONAL MATCH (file)<-[:MODIFIES]-(r:Repair {verdict: 'VERIFIED'})
RETURN t.name AS failing_test, fn.name AS function, file.path AS file,
       count(r) AS past_verified_repairs
ORDER BY failing_test"""


class GraphStore:
    """Neo4j code graph: (File)-[:DEFINES]->(Function), (File)-[:IMPORTS]->(File),
    (Test)-[:TARGETS]->(Function), (Failure)-[:OCCURS_IN]->(Test),
    (Repair)-[:MODIFIES]->(File), (Repair)-[:ADDRESSES]->(Failure)."""

    def __init__(self, driver):
        self.driver = driver

    def _run(self, query, **params):
        with self.driver.session() as session:
            return [r.data() for r in session.run(query, **params)]

    def record_code(self, g):
        self._run("UNWIND $files AS f MERGE (:File {path: f})", files=g["files"])
        self._run("UNWIND $defs AS d MATCH (file:File {path: d.file}) "
                  "MERGE (fn:Function {name: d.name, file: d.file}) MERGE (file)-[:DEFINES]->(fn)",
                  defs=g["defs"])
        self._run("UNWIND $imports AS i MATCH (a:File {path: i.src}), (b:File {path: i.dst}) "
                  "MERGE (a)-[:IMPORTS]->(b)", imports=g["imports"])
        self._run("UNWIND $tests AS t MERGE (test:Test {name: t.name, file: t.file}) "
                  "WITH test, t UNWIND t.targets AS target "
                  "MATCH (fn:Function {name: target}) MERGE (test)-[:TARGETS]->(fn)",
                  tests=g["tests"])

    def record_failures(self, run_id, failures):
        self._run("UNWIND $fails AS f MATCH (t:Test {name: f.test, file: f.file}) "
                  "CREATE (:Failure {run_id: $run_id, test: f.test, created_at: datetime()})"
                  "-[:OCCURS_IN]->(t)", run_id=run_id, fails=failures)

    def context(self, run_id):
        return GRAPH_CONTEXT_QUERY, self._run(GRAPH_CONTEXT_QUERY, run_id=run_id)

    def record_repair(self, run_id, path, verdict, diagnosis, model):
        self._run("CREATE (r:Repair {run_id: $run_id, verdict: $verdict, diagnosis: $diagnosis, "
                  "model: $model, created_at: datetime()}) "
                  "WITH r MATCH (file:File {path: $path}) MERGE (r)-[:MODIFIES]->(file) "
                  "WITH r MATCH (x:Failure {run_id: $run_id}) MERGE (r)-[:ADDRESSES]->(x)",
                  run_id=run_id, path=path, verdict=verdict, diagnosis=diagnosis, model=model)

    def close(self):
        self.driver.close()


def format_graph_context(rows):
    if not rows:
        return ""
    lines = [f"- {r['failing_test']} targets {r['function']}() defined in {r['file']}"
             f" ({r['past_verified_repairs']} past verified repairs)" for r in rows]
    return "Code graph context from Neo4j:\n" + "\n".join(lines) + "\n\n"


def build_messages(sources, failing_output, graph_rows=None):
    system = (
        "You repair Python bugs. Reply with ONLY one JSON object: no prose, no markdown. "
        'Schema: {"diagnosis": str, "explanation": str, '
        '"edits": [{"path": "pricing.py", "old": str, "new": str}]}. '
        "Rules: exactly one edit; edit only pricing.py; never edit tests; "
        "'old' must be copied exactly from pricing.py and appear there exactly once; "
        "keep the change minimal."
    )
    user = (
        f"Failing pytest output:\n{failing_output[-MAX_OUTPUT:]}\n\n"
        f"{format_graph_context(graph_rows)}"
        f"pricing.py (you may edit this):\n{sources[IMPL_FILE]}\n\n"
        f"test_pricing.py (read-only, do not change):\n{sources[TEST_FILE]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------- provider adapters ----------

def make_daytona():
    from daytona import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"],
                                 target=os.getenv("DAYTONA_TARGET", "us")))


def make_graph():
    """A connected GraphStore, or None when Neo4j isn't configured."""
    keys = ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD")
    if not all(os.getenv(k) for k in keys):
        return None
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"],
                                  auth=(os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"]),
                                  connection_timeout=10)
    try:
        driver.verify_connectivity()
    except Exception:
        driver.close()
        raise
    return GraphStore(driver)


def make_llm():
    from openai import OpenAI
    return OpenAI(base_url=NOSANA_BASE_URL, api_key=os.environ["NOSANA_API_KEY"],
                  timeout=NOSANA_TIMEOUT, max_retries=0)


NON_CHAT_HINTS = ("embed", "rerank")


def chat_models(ids):
    """Drop embedding/reranking models, which cannot generate a repair."""
    return [i for i in ids if not any(h in i.lower() for h in NON_CHAT_HINTS)]


def list_models(llm=None):
    llm = llm or make_llm()
    return chat_models([m.id for m in llm.models.list().data])


def call_model(llm, model, messages):
    reply = llm.chat.completions.create(model=model, messages=messages,
                                        temperature=0, max_tokens=2000)
    msg = reply.choices[0].message
    if not (msg.content or "").strip():
        hint = " (it returned only reasoning; pick another model)" if getattr(msg, "reasoning", None) else ""
        raise EditRejected(f"model returned an empty answer{hint}")
    return msg.content


def _exec(sandbox, cmd, cwd=None, timeout=CMD_TIMEOUT):
    kwargs = {"timeout": timeout}
    if cwd:
        kwargs["cwd"] = cwd
    r = sandbox.process.exec(cmd, **kwargs)
    return r.exit_code, (r.result or "")


def prepare_pytest(sandbox, run):
    """Find a Python and make sure pytest is available. Returns the test command."""
    py = None
    for candidate in ("python", "python3"):
        code, _ = _exec(sandbox, f"{candidate} --version")
        if code == 0:
            py = candidate
            break
    if py is None:
        raise RuntimeError("no python interpreter found in sandbox")
    if _exec(sandbox, f"{py} -m pytest --version")[0] != 0:
        run.log("reproducing", "pytest missing in sandbox, installing")
        _exec(sandbox, f"{py} -m pip install -q pytest", timeout=SETUP_TIMEOUT)
        if _exec(sandbox, f"{py} -m pytest --version")[0] != 0:
            raise RuntimeError("pytest is not available in the sandbox and could not be "
                               "installed (the sandbox may have no internet access)")
    return f"{py} -m pytest -q"


# ---------- the repair run ----------

def _graph_context(run, sources, graph):
    """Write the code graph and this run's failures to Neo4j, then query the failure's
    neighbourhood. Never raises: on any problem the repair simply continues without it."""
    try:
        if graph == "auto":
            graph = make_graph()
        if graph is None:
            run.graph_status = "not configured (repair continues without graph context)"
            return None
        run.log("investigating", "Writing code graph and failures to Neo4j")
        graph.record_code(build_code_graph(sources))
        graph.record_failures(run.run_id, parse_failing_tests(run.baseline_output))
        run.graph_query, run.graph_rows = graph.context(run.run_id)
        run.graph_status = f"connected: {len(run.graph_rows)} related row(s) found"
        run.log("investigating", run.graph_status)
        return graph
    except Exception as e:
        run.graph_status = (f"unavailable ({type(e).__name__}: {str(e)[:120]}); "
                            "repair continues without graph context")
        run.log("investigating", "Neo4j unavailable, continuing without graph context")
        return None


def run_repair(model, on_event=None, daytona=None, llm=None, graph="auto"):
    run = Run(model=model, on_event=on_event)
    sources = load_fixture()  # local fixture is only read, never written
    sandbox = None
    try:
        run.log("reproducing", "Creating Daytona sandbox")
        sandbox = (daytona or make_daytona()).create()
        run.sandbox_id = sandbox.id
        _exec(sandbox, f"mkdir -p {REMOTE_DIR}")
        for name, text in sources.items():
            sandbox.fs.upload_file(text.encode(), f"{REMOTE_DIR}/{name}")
        run.test_command = prepare_pytest(sandbox, run)

        run.log("reproducing", f"Baseline: {run.test_command}")
        run.baseline_exit, run.baseline_output = _exec(sandbox, run.test_command, cwd=REMOTE_DIR)
        verdict = baseline_verdict(run.baseline_exit)
        if verdict == NO_FAILURE:
            run.verdict = NO_FAILURE
            return run
        if verdict == ERROR:
            run.verdict = ERROR
            run.error = (f"baseline pytest exited with code {run.baseline_exit}: "
                         "an execution error, not a reproduced bug")
            return run

        graph = _graph_context(run, sources, graph)

        run.log("proposing", f"Asking Nosana model {model}")
        raw = call_model(llm or make_llm(), model,
                         build_messages(sources, run.baseline_output, run.graph_rows))
        run.raw_model_output = raw
        data = extract_json(raw)
        path, new_source = validate_proposal(data, sources)
        run.diagnosis, run.explanation = data["diagnosis"], data["explanation"]
        run.diff = make_diff(path, sources[path], new_source)

        run.log("applying", f"Applying validated edit to {path} inside the sandbox")
        sandbox.fs.upload_file(new_source.encode(), f"{REMOTE_DIR}/{path}")
        # re-upload the original tests so the rerun is guaranteed to use unchanged tests
        sandbox.fs.upload_file(sources[TEST_FILE].encode(), f"{REMOTE_DIR}/{TEST_FILE}")

        run.log("verifying", f"Rerun: {run.test_command}")
        run.rerun_exit, run.rerun_output = _exec(sandbox, run.test_command, cwd=REMOTE_DIR)
        run.verdict = rerun_verdict(run.rerun_exit)
        if run.verdict == ERROR:
            run.error = f"rerun pytest exited with code {run.rerun_exit}: execution error"
        elif graph is not None:
            try:
                graph.record_repair(run.run_id, path, run.verdict, run.diagnosis, model)
                run.log("verifying", "Recorded the repair outcome in Neo4j")
            except Exception as e:
                run.graph_status += f"; saving the repair failed ({type(e).__name__})"
    except EditRejected as e:
        run.verdict, run.error = ERROR, f"proposal rejected, nothing applied: {e}"
    except Exception as e:
        run.verdict, run.error = ERROR, f"{type(e).__name__}: {e}"[:600]
    finally:
        if graph is not None and hasattr(graph, "close"):
            try:
                graph.close()
            except Exception:
                pass
        if sandbox is not None:
            try:
                sandbox.delete()
                run.cleanup = "sandbox deleted"
            except Exception as e:
                run.cleanup = (f"CLEANUP FAILED ({type(e).__name__}); delete sandbox "
                               f"{run.sandbox_id} from the Daytona dashboard")
        run.log("done", f"Verdict: {run.verdict}")
    return run
