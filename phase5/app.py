"""
BQ-Vertex-Analyst -- Phase 5: Streamlit Demo UI
==================================================

Deliberately thin. This calls the FastAPI service over HTTP (localhost by
default) -- it does NOT import agent_core or any phase2-4 code directly.
That's intentional: the UI is a client of the API, the same way any real
frontend would be, not a shortcut that bypasses the service layer.

Prerequisites
-------------
    pip install streamlit requests
    # In a separate terminal, from the repo root:
    python -m uvicorn phase5.service:app --port 8000

Usage
-----
    streamlit run app.py
"""

import pandas as pd
import requests
import streamlit as st

API_BASE = "http://127.0.0.1:8000"

st.set_page_config(page_title="BQ-Vertex-Analyst", layout="centered", page_icon=":bar_chart:")

# Small CSS pass on top of the .streamlit/config.toml theme -- config.toml
# sets the base palette (reused from the portfolio site's own colors, not
# invented fresh); this adds a couple of things Streamlit's theme system
# doesn't expose directly (monospace headers, a subtle border on the
# result card, badge pill styling for gate/hallucination status).
st.markdown(
    """
    <style>
    h1, h2, h3 { font-family: 'JetBrains Mono', monospace; }
    .result-card {
        border: 1px solid #232B3D;
        border-radius: 8px;
        padding: 1.2rem 1.4rem;
        background: #121826;
        margin-top: 0.5rem;
        margin-bottom: 0.5rem;
    }
    .badge {
        display: inline-block;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.78rem;
        padding: 0.25rem 0.7rem;
        border-radius: 999px;
        font-weight: 600;
        margin-bottom: 0.6rem;
    }
    .badge-pass { background: rgba(95,191,119,0.15); color: #5FBF77; }
    .badge-fail { background: rgba(229,83,75,0.15); color: #E5534B; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("BQ-Vertex-Analyst")
st.caption("Schema-aware SQL agent over TheLook eCommerce, with a real validation gate in front of it.")

tab_explore, tab_answer, tab_suggest = st.tabs(["Explore data", "Ask a question", "Suggest questions"])

with tab_explore:
    st.caption("See what tables and columns exist before asking a question -- and preview real rows.")
    try:
        schema_resp = requests.get(f"{API_BASE}/schema", timeout=10)
    except requests.exceptions.ConnectionError:
        st.error(
            "Could not reach the API service. Is it running? "
            "Start it with: python -m uvicorn phase5.service:app --port 8000"
        )
        st.stop()

    if schema_resp.status_code != 200:
        st.error(f"Could not load schema: {schema_resp.text}")
    else:
        tables = schema_resp.json().get("tables", [])
        for t in tables:
            with st.expander(f"{t['table']}  ({t['row_count']:,} rows)"):
                st.markdown(
                    f'<span style="color:#F0A93E; font-weight:600; font-family:monospace;">{t["table"]}</span>'
                    f'  <span style="color:#4FD1C5; font-family:monospace;">{t["row_count"]:,} rows</span>',
                    unsafe_allow_html=True,
                )

                schema_rows = []
                for col in t["columns"]:
                    notes = []
                    if col.get("cardinality_reliable") is False:
                        notes.append("cardinality unknown")
                    if col.get("inferred_fk_target"):
                        notes.append(f"FK -> {col['inferred_fk_target']}")
                    schema_rows.append({
                        "Column": col["name"],
                        "Type": col["type"],
                        "Notes": ", ".join(notes),
                    })

                schema_df = pd.DataFrame(schema_rows)

                # Reuses the portfolio site's own palette (cyan for numeric/
                # structural types, amber for text, red for anything flagged
                # in Notes) -- color communicates real information here, not
                # just decoration: a red Notes cell is the same signal the
                # grounding gate enforces in the backend, now visible here too.
                TYPE_COLORS = {
                    "INT64": "#4FD1C5", "FLOAT64": "#4FD1C5",
                    "STRING": "#F0A93E", "TIMESTAMP": "#B8801F",
                    "GEOGRAPHY": "#E5534B",
                }

                def _color_type(val):
                    return f"color: {TYPE_COLORS.get(val, '#E6E8EB')}; font-weight: 600"

                def _color_notes(val):
                    return "color: #E5534B; font-weight: 600" if val else ""

                styled_schema = schema_df.style.map(_color_type, subset=["Type"]).map(_color_notes, subset=["Notes"])

                st.dataframe(
                    styled_schema,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Notes": st.column_config.TextColumn(width="large"),
                    },
                )

                if st.button("Preview sample rows", key=f"preview_{t['table']}"):
                    with st.spinner("Fetching sample rows..."):
                        try:
                            sample_resp = requests.get(f"{API_BASE}/sample/{t['table']}", timeout=30)
                        except requests.exceptions.ConnectionError:
                            st.error("Could not reach the API service.")
                            sample_resp = None
                    if sample_resp is not None:
                        if sample_resp.status_code != 200:
                            st.error(f"Preview failed: {sample_resp.json().get('detail', sample_resp.text)}")
                        else:
                            rows = sample_resp.json().get("rows", [])
                            if rows:
                                # Transposed on purpose: some tables here have 12-16 columns,
                                # which forces horizontal scrolling and hides fields in a
                                # normal orientation. Field names down the left, one sample
                                # record per column, is far more readable for wide tables.
                                df = pd.DataFrame(rows)
                                df.index = [f"Row {i + 1}" for i in range(len(df))]
                                transposed = df.T
                                # Explicit width per record column -- default auto-sizing
                                # clipped long values (emails, addresses) to fit narrow
                                # columns. "large" gives enough room for a full email
                                # address without truncation in the common case.
                                column_config = {
                                    col: st.column_config.TextColumn(width="large")
                                    for col in transposed.columns
                                }
                                st.dataframe(transposed, use_container_width=True, column_config=column_config)
                            else:
                                st.info("No rows returned.")

with tab_answer:
    question = st.text_input(
        "Ask a question about the data",
        placeholder="e.g. What is the average order value by state?",
    )
    if st.button("Ask", key="answer_btn") and question.strip():
        with st.spinner("Generating SQL, validating, executing..."):
            try:
                resp = requests.post(f"{API_BASE}/answer", json={"question": question}, timeout=60)
            except requests.exceptions.ConnectionError:
                st.error(
                    "Could not reach the API service. Is it running? "
                    "Start it with: python -m uvicorn phase5.service:app --port 8000"
                )
                st.stop()

        if resp.status_code != 200:
            st.error(f"Request failed ({resp.status_code}): {resp.json().get('detail', resp.text)}")
        else:
            data = resp.json()

            gate_passed = data.get("gate_passed")
            badge_class = "badge-pass" if gate_passed else "badge-fail"
            badge_text = "GATE: PASSED" if gate_passed else "GATE: BLOCKED"
            st.markdown(f'<span class="badge {badge_class}">{badge_text}</span>', unsafe_allow_html=True)

            st.markdown('<div class="result-card">', unsafe_allow_html=True)
            st.subheader("Generated SQL")
            st.code(data.get("sql", ""), language="sql")

            if data.get("caveats"):
                st.caption("Caveats from the agent:")
                for c in data["caveats"]:
                    st.caption(f"- {c}")
            st.markdown("</div>", unsafe_allow_html=True)

            if not gate_passed:
                st.markdown('<div class="result-card">', unsafe_allow_html=True)
                st.markdown("**Blocking violations**")
                for v in data.get("gate_blocking", []):
                    st.write(f"- {v}")
                st.markdown("</div>", unsafe_allow_html=True)

            if data.get("grounded_answer"):
                st.markdown('<div class="result-card">', unsafe_allow_html=True)
                st.subheader("Answer")
                st.write(data["grounded_answer"])

                col1, col2, col3 = st.columns(3)
                col1.metric("Rows returned", data.get("execution_row_count", "?"))
                bytes_billed = data.get("execution_bytes_billed", 0) or 0
                col2.metric("Bytes billed", f"{bytes_billed:,}")
                hallucination_ok = data.get("hallucination_passed")
                col3.metric("Hallucination check", "PASSED" if hallucination_ok else "FAILED")
                st.markdown("</div>", unsafe_allow_html=True)

            if data.get("warnings"):
                with st.expander(f"Warnings ({len(data['warnings'])})"):
                    for w in data["warnings"]:
                        st.write(f"- {w}")

with tab_suggest:
    if st.button("Suggest questions", key="suggest_btn"):
        with st.spinner("Generating ranked questions..."):
            try:
                resp = requests.post(f"{API_BASE}/suggest", timeout=60)
            except requests.exceptions.ConnectionError:
                st.error(
                    "Could not reach the API service. Is it running? "
                    "Start it with: python -m uvicorn phase5.service:app --port 8000"
                )
                st.stop()

        if resp.status_code != 200:
            st.error(f"Request failed ({resp.status_code}): {resp.json().get('detail', resp.text)}")
        else:
            data = resp.json()
            for i, q in enumerate(data.get("questions", []), 1):
                st.markdown('<div class="result-card">', unsafe_allow_html=True)
                st.markdown(f"**{i}. {q.get('question', '')}**")
                st.caption(q.get("rationale", ""))
                st.caption(f"Tables: {', '.join(q.get('relevant_tables', []))}")
                st.markdown("</div>", unsafe_allow_html=True)