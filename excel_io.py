from __future__ import annotations

from io import BytesIO
from typing import Dict

import pandas as pd

from scheduler_core import REQUIRED_SHEETS, normalize_tables


def sample_tables() -> Dict[str, pd.DataFrame]:
    return {
        "Leagues": pd.DataFrame([
            {"LeagueID": "D1", "LeagueName": "Division 1", "StartDate": "2026-06-01", "Weeks": 10, "GamesPerNight": 2, "DayOfWeek": "Monday", "LevelGroup": "Adult", "Status": "Active"},
            {"LeagueID": "D2", "LeagueName": "Division 2", "StartDate": "2026-06-03", "Weeks": 10, "GamesPerNight": 2, "DayOfWeek": "Wednesday", "LevelGroup": "Adult", "Status": "Active"},
        ]),
        "Teams": pd.DataFrame([
            {"LeagueID": "D1", "TeamID": "D1-T1", "TeamName": "Sharks", "Active": True},
            {"LeagueID": "D1", "TeamID": "D1-T2", "TeamName": "Barracuda", "Active": True},
            {"LeagueID": "D1", "TeamID": "D1-T3", "TeamName": "Seals", "Active": True},
            {"LeagueID": "D1", "TeamID": "D1-T4", "TeamName": "Blades", "Active": True},
            {"LeagueID": "D2", "TeamID": "D2-T1", "TeamName": "Ice Hawks", "Active": True},
            {"LeagueID": "D2", "TeamID": "D2-T2", "TeamName": "NorCal Bears", "Active": True},
            {"LeagueID": "D2", "TeamID": "D2-T3", "TeamName": "Renegades", "Active": True},
            {"LeagueID": "D2", "TeamID": "D2-T4", "TeamName": "Vipers", "Active": True},
        ]),
        "TimeSlots": pd.DataFrame([
            {"LeagueID": "D1", "SlotOrder": 1, "Time": "7:00 PM"},
            {"LeagueID": "D1", "SlotOrder": 2, "Time": "8:15 PM"},
            {"LeagueID": "D2", "SlotOrder": 1, "Time": "7:00 PM"},
            {"LeagueID": "D2", "SlotOrder": 2, "Time": "8:15 PM"},
        ]),
        "Blackouts": pd.DataFrame([
            {"LeagueID": "*", "StartDate": "2026-07-01", "EndDate": "2026-07-07", "Type": "Holiday", "Reason": "July break"},
        ]),
    }


def load_excel(file_obj) -> Dict[str, pd.DataFrame]:
    workbook = pd.read_excel(file_obj, sheet_name=None)
    return normalize_tables(workbook)


def tables_to_excel_bytes(
    tables: Dict[str, pd.DataFrame],
    schedule: pd.DataFrame | None = None,
    audit: pd.DataFrame | None = None,
    summary: pd.DataFrame | None = None,
) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        normalized = normalize_tables(tables)
        for sheet_name, df in normalized.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)
            _format_sheet(writer, sheet_name, df)

        if schedule is not None:
            schedule.to_excel(writer, sheet_name="Schedule", index=False)
            _format_sheet(writer, "Schedule", schedule)
        if summary is not None:
            summary.to_excel(writer, sheet_name="Summary", index=False)
            _format_sheet(writer, "Summary", summary)
        if audit is not None:
            audit.to_excel(writer, sheet_name="Audit", index=False)
            _format_sheet(writer, "Audit", audit)

    return output.getvalue()


def _format_sheet(writer, sheet_name: str, df: pd.DataFrame) -> None:
    workbook = writer.book
    worksheet = writer.sheets[sheet_name]
    header_format = workbook.add_format({"bold": True, "bg_color": "#1F4E78", "font_color": "#FFFFFF", "border": 1})
    body_format = workbook.add_format({"border": 1})
    for col_idx, col_name in enumerate(df.columns):
        worksheet.write(0, col_idx, col_name, header_format)
        width = min(max(len(str(col_name)) + 4, 12), 32)
        if not df.empty:
            width = min(max(width, int(df[col_name].astype(str).str.len().quantile(0.9)) + 2), 40)
        worksheet.set_column(col_idx, col_idx, width, body_format)
    worksheet.freeze_panes(1, 0)
    if len(df.index) > 0 and len(df.columns) > 0:
        worksheet.autofilter(0, 0, len(df.index), len(df.columns) - 1)
