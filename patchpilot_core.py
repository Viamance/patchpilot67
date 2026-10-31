"""PatchPilot coordinator.

Flow: Daytona baseline -> Nosana proposal -> Daytona verification.
One fixture, one attempt per run, no retry loop. See mvp.md for the spec.
"""

import difflib
import json
import os
import re
import time
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


def build_messages(sources, failing_output):
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
        f"pricing.py (you may edit this):\n{sources[IMPL_FILE]}\n\n"
        f"test_pricing.py (read-only, do not change):\n{sources[TEST_FILE]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------- provider adapters ----------

def make_daytona():
    from daytona import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=os.environ["DAYTONA_API_KEY"],
                                 target=os.getenv("DAYTONA_TARGET", "us")))


def make_llm():
    from openai import OpenAI
    return OpenAI(base_url=NOSANA_BASE_URL, api_key=os.environ["NOSANA_API_KEY"],
                  timeout=NOSANA_TIMEOUT, max_retries=0)


def list_models(llm=None):
    llm = llm or make_llm()
    return [m.id for m in llm.models.list().data]


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

def run_repair(model, on_event=None, daytona=None, llm=None):
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

        run.log("proposing", f"Asking Nosana model {model}")
        raw = call_model(llm or make_llm(), model, build_messages(sources, run.baseline_output))
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
    except EditRejected as e:
        run.verdict, run.error = ERROR, f"proposal rejected, nothing applied: {e}"
    except Exception as e:
        run.verdict, run.error = ERROR, f"{type(e).__name__}: {e}"[:600]
    finally:
        if sandbox is not None:
            try:
                sandbox.delete()
                run.cleanup = "sandbox deleted"
            except Exception as e:
                run.cleanup = (f"CLEANUP FAILED ({type(e).__name__}); delete sandbox "
                               f"{run.sandbox_id} from the Daytona dashboard")
        run.log("done", f"Verdict: {run.verdict}")
    return run
