"""PatchPilot: one-screen Streamlit app. Launch with: python -m streamlit run app.py"""

import streamlit as st
from dotenv import load_dotenv

import patchpilot_core as core

load_dotenv()
st.set_page_config(page_title="PatchPilot", page_icon="🩹", layout="wide")

st.title("PatchPilot")
st.caption("From a failing test to an AI-proposed, sandbox-verified patch, with the evidence visible.")

VERDICT_STYLE = {
    core.VERIFIED: st.success,
    core.NOT_VERIFIED: st.warning,
    core.NO_FAILURE: st.info,
    core.ERROR: st.error,
}


@st.cache_data(ttl=300, show_spinner=False)
def available_models():
    return core.list_models()


# ---- sidebar: provider check and model choice ----
with st.sidebar:
    st.header("Providers")
    try:
        models = available_models()
    except Exception as e:
        models = []
        st.error(f"Nosana model list failed: {type(e).__name__}: {str(e)[:200]}")
    if models:
        model = st.selectbox("Nosana model", models)
        st.caption(f"{len(models)} model(s) currently served")
    else:
        model = None
        st.warning("No Nosana model available, so a repair cannot run.")
    if st.button("Refresh models"):
        available_models.clear()
        st.rerun()
    st.divider()
    st.caption("Daytona runs the code · Nosana proposes the fix · tests decide the verdict")

# ---- the prepared fixture ----
sources = core.load_fixture()
with st.expander("Prepared fixture (seeded demo defect)", expanded=True):
    left, right = st.columns(2)
    left.markdown(f"**{core.IMPL_FILE}** (editable by PatchPilot)")
    left.code(sources[core.IMPL_FILE], language="python")
    right.markdown(f"**{core.TEST_FILE}** (read-only)")
    right.code(sources[core.TEST_FILE], language="python")

# ---- run ----
if st.button("Repair demo bug", type="primary", disabled=model is None):
    with st.status("Running repair...", expanded=True) as status:
        def on_event(stage, message):
            status.update(label=f"{stage.capitalize()}...")
            st.write(f"**{stage}**: {message}")
        st.session_state["run"] = core.run_repair(model, on_event=on_event)
        status.update(label="Run finished", state="complete", expanded=False)

run = st.session_state.get("run")
if run:
    VERDICT_STYLE.get(run.verdict, st.error)(f"**{run.verdict}**" + (f": {run.error}" if run.error else ""))

    before, after = st.columns(2)
    with before:
        st.subheader("1. Baseline (before)")
        st.caption(f"`{run.test_command}` · exit code {run.baseline_exit}")
        st.code(run.baseline_output or "(no output)", language="text")
    with after:
        st.subheader("3. Rerun (after)")
        if run.rerun_exit is None:
            st.caption("Not run")
        else:
            st.caption(f"`{run.test_command}` · exit code {run.rerun_exit} · same tests, unchanged")
            st.code(run.rerun_output or "(no output)", language="text")

    st.subheader("2. AI proposal (suggested change)")
    if run.diagnosis:
        st.markdown(f"**Diagnosis:** {run.diagnosis}")
        st.markdown(f"**Explanation:** {run.explanation}")
    if run.diff:
        st.code(run.diff, language="diff")
    elif run.verdict != core.NO_FAILURE:
        st.caption("No patch was applied.")

    with st.expander("Run evidence"):
        st.write({
            "Nosana model": run.model,
            "Daytona sandbox": run.sandbox_id,
            "Cleanup": run.cleanup,
            "Baseline exit": run.baseline_exit,
            "Rerun exit": run.rerun_exit,
        })
        st.table([{"time": t, "stage": s, "event": m} for t, s, m in run.events])
        if run.raw_model_output:
            st.markdown("**Raw model response**")
            st.code(run.raw_model_output, language="json")
    st.caption("VERIFIED means the designated tests passed after the patch; it does not prove "
               "the code is bug-free. The local fixture file is never modified.")
