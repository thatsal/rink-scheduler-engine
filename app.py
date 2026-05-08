from __future__ import annotations

import streamlit as st
import pandas as pd

from excel_io import load_excel, sample_tables, tables_to_excel_bytes
from scheduler_core import REQUIRED_SHEETS, build_all_schedules, normalize_tables, get_empty_tables

st.set_page_config(page_title="Nor-Cal Hockey Scheduler", page_icon="🏒", layout="wide")

st.title("🏒 Nor-Cal Hockey Scheduler MVP")
st.caption("Python + Streamlit version. Upload/edit Excel tables, generate schedules, review audit warnings, download results.")

if "tables" not in st.session_state:
    st.session_state.tables = sample_tables()

with st.sidebar:
    st.header("Workflow")
    st.markdown(
        """
1. Download the template workbook, or use your own workbook.  
2. Fill in leagues, teams, time slots, and blackouts.  
3. Upload the workbook.  
4. Edit tables if needed.  
5. Generate schedules.  
6. Download the finished workbook.
        """
    )

    template_bytes = tables_to_excel_bytes(get_empty_tables())
    st.download_button(
        "📥 Download blank template",
        data=template_bytes,
        file_name="norcal_scheduler_template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    current_bytes = tables_to_excel_bytes(st.session_state.tables)
    st.download_button(
        "📥 Download sample/current workbook",
        data=current_bytes,
        file_name="norcal_scheduler_inputs.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    uploaded = st.file_uploader("Upload Excel workbook", type=["xlsx"])

if uploaded is not None:
    try:
        st.session_state.tables = load_excel(uploaded)
        st.success("Workbook loaded.")
    except Exception as exc:
        st.error(f"Could not load workbook: {exc}")

st.subheader("Setup Tables")
st.info("Edit the tables below or upload a completed workbook. These replace the old Google Sheets/App Script editing layer.")

tabs = st.tabs(list(REQUIRED_SHEETS.keys()))
updated_tables = {}

for tab, sheet_name in zip(tabs, REQUIRED_SHEETS.keys()):
    with tab:
        df = st.session_state.tables.get(sheet_name, pd.DataFrame(columns=REQUIRED_SHEETS[sheet_name]))
        st.write(f"Edit **{sheet_name}**")
        updated = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            key=f"editor_{sheet_name}",
        )
        updated_tables[sheet_name] = updated

st.session_state.tables = normalize_tables(updated_tables)

left, right = st.columns([1, 2])

with left:
    generate = st.button("Generate Schedules", type="primary", use_container_width=True)

with right:
    input_bytes = tables_to_excel_bytes(st.session_state.tables)
    st.download_button(
        "Download current input workbook",
        data=input_bytes,
        file_name="norcal_scheduler_inputs.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

if generate:
    schedule, audit = build_all_schedules(st.session_state.tables)
    st.session_state.schedule = schedule
    st.session_state.audit = audit

if "schedule" in st.session_state:
    st.subheader("Generated Schedule")
    st.dataframe(st.session_state.schedule, use_container_width=True, hide_index=True)

    st.subheader("Audit")
    audit = st.session_state.audit

    if audit.empty:
        st.success("No audit warnings generated.")
    else:
        severity_filter = st.multiselect(
            "Filter audit severity",
            options=sorted(audit["Severity"].dropna().unique()),
            default=sorted(audit["Severity"].dropna().unique()),
        )
        filtered = audit[audit["Severity"].isin(severity_filter)] if severity_filter else audit
        st.dataframe(filtered, use_container_width=True, hide_index=True)

    output_bytes = tables_to_excel_bytes(
        st.session_state.tables,
        st.session_state.schedule,
        st.session_state.audit,
    )

    st.download_button(
        "Download schedule workbook",
        data=output_bytes,
        file_name="norcal_generated_schedule.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        use_container_width=True,
    )
else:
    st.subheader("Expected workbook sheets")
    st.write({sheet: cols for sheet, cols in REQUIRED_SHEETS.items()})
