"""RAG context panel — inline Q&A linked to the digital twin."""

import streamlit as st
from typing import Optional


def render_rag_panel(twin, snapshot=None) -> None:
    """Render the RAG-powered RCA panel inside the dashboard."""
    st.subheader("RAG-Powered Root Cause Analysis")

    if snapshot and snapshot.rag_context:
        with st.expander("Auto-generated RCA context (last prediction)", expanded=True):
            st.markdown(snapshot.rag_context)

    st.markdown("---")
    st.caption("Ask a question about the Aviator assembly process, component specs, or failure history.")

    question = st.text_input(
        "Question",
        placeholder="e.g. What causes RF path mismatch on the antenna pins?",
        key="rag_question_input",
    )

    if st.button("Ask RAG", key="rag_submit"):
        if not question.strip():
            st.warning("Enter a question first.")
            return

        if twin._rag is None:
            st.error(
                "RAG pipeline not connected. Launch the dashboard with `--with-rag` or "
                "initialise `DigitalTwin(rag_pipeline=RAGPipeline())`."
            )
            return

        with st.spinner("Retrieving…"):
            answer = twin.query_with_context(question)

        st.success("**Answer**")
        st.markdown(answer)
