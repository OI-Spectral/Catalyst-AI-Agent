import os
import json

#read .env before the other modules read their settings from the environment
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import streamlit as st

from pipeline import Pipeline, GateStopped
from state import RunStopped, slug
from report import render_markdown, render_run_notes
from google_doc import create_google_doc, GoogleDocsNotSetUp


#web demo: streamlit run app.py
st.set_page_config(page_title = "Philosopher Verdict", page_icon = "⚖️")
st.title("Philosopher Verdict")
st.caption("Type a philosopher's name. Get their life, their most famous idea, the strongest case for and against it, "
           "and a reasoned verdict on how good a philosopher they were. Every claim links to a source.")

with st.form("lookup"):
    name = st.text_input("Philosopher", placeholder = "Immanuel Kant", max_chars = 80)
    submitted = st.form_submit_button("Get the verdict")

#a name picked from an ambiguous result's options runs as if it had been typed
picked = st.session_state.pop("picked", None)
target = picked or (name.strip() if submitted else "")

if target:
    for key in ("report", "options", "doc_url"):
        st.session_state.pop(key, None)
    with st.status(f"Working on {target}…", expanded = True) as status:
        try:
            report = Pipeline(target, log = status.write).run()
            status.update(label = f"Done in {report['usage']['seconds']:.0f} s for ${report['usage']['cost_usd']:.2f}",
                          state = "complete", expanded = False)
            st.session_state["report"] = report
        except GateStopped as e:
            status.update(label = "Stopped at the validity gate", state = "error")
            (st.warning if e.outcome == "ambiguous" else st.error)(e.message)
            st.session_state["options"] = e.options
        except RunStopped as e:
            status.update(label = "Stopped before the research finished", state = "error")
            st.error(str(e))

options = st.session_state.get("options") or []
if options:
    st.write("Did you mean:")
    for option in options:
        if st.button(option, key = f"option-{option}"):
            st.session_state["picked"] = option
            st.rerun()

report = st.session_state.get("report")
if report:
    if report.get("panel"):
        st.metric("Weighted score", f"{report['panel']['weighted_score']} / 10")
    st.markdown(render_markdown(report))
    if st.button("Save as a Google Doc"):
        try:
            st.session_state["doc_url"] = create_google_doc(report)
        except GoogleDocsNotSetUp as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Couldn't create the Google Doc ({type(e).__name__}: {e}).")
    if st.session_state.get("doc_url"):
        st.success(f"[Open the Google Doc]({st.session_state['doc_url']})")
    with st.expander("Run notes"):
        st.code(render_run_notes(report), language = None)
    st.download_button("Download the report as JSON", json.dumps(report, indent = 2, ensure_ascii = False),
                       file_name = f"{slug(report['philosopher'])}.json", mime = "application/json")
