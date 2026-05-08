from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Any

import pandas as pd

REQUIRED_SHEETS = {
    "Leagues": ["LeagueID", "LeagueName", "StartDate", "Weeks", "GamesPerNight", "DayOfWeek", "LevelGroup", "Status"],
    "Teams": ["LeagueID", "TeamID", "TeamName", "Active"],
    "TimeSlots": ["LeagueID", "SlotOrder", "Time"],
    "Blackouts": ["LeagueID", "StartDate", "EndDate", "Type", "Reason"],
}

SCHEDULE_COLUMNS = [
    "LeagueID", "LeagueName", "Week", "Date", "Time", "SlotOrder",
    "HomeTeamID", "HomeTeam", "AwayTeamID", "AwayTeam", "Notes", "Flag", "FlagReason",
]

AUDIT_COLUMNS = ["LeagueID", "LeagueName", "Severity", "Week", "Date", "Time", "Issue", "Details", "RowRef"]


def get_empty_tables() -> Dict[str, pd.DataFrame]:
    return {
        sheet_name: pd.DataFrame(columns=columns)
        for sheet_name, columns in REQUIRED_SHEETS.items()
    }


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "y", "1", "active"}


def clean_str(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def as_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def as_time_label(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, datetime):
        return value.strftime("%I:%M %p").lstrip("0")
    parsed = pd.to_datetime(value, errors="coerce")
    if not pd.isna(parsed):
        return parsed.strftime("%I:%M %p").lstrip("0")
    return str(value).strip()


def normalize_tables(tables: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    normalized: Dict[str, pd.DataFrame] = {}
    for sheet, cols in REQUIRED_SHEETS.items():
        df = tables.get(sheet, pd.DataFrame(columns=cols)).copy()
        for col in cols:
            if col not in df.columns:
                df[col] = None
        normalized[sheet] = df[cols].copy()
    return normalized


def round_robin_pairs(team_ids: List[str]) -> List[List[Tuple[str, str]]]:
    teams = list(team_ids)
    if len(teams) < 2:
        return []
    if len(teams) % 2 == 1:
        teams.append("BYE")

    n = len(teams)
    rounds: List[List[Tuple[str, str]]] = []
    rotation = teams[:]

    for round_index in range(n - 1):
        pairs: List[Tuple[str, str]] = []
        for i in range(n // 2):
            a = rotation[i]
            b = rotation[n - 1 - i]
            if "BYE" not in (a, b):
                pairs.append((a, b) if round_index % 2 == 0 else (b, a))
        rounds.append(pairs)
        rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]
    return rounds


def is_blackout(check_date: date, blackouts: pd.DataFrame, league_id: str) -> Tuple[bool, str]:
    for _, row in blackouts.iterrows():
        blackout_league = clean_str(row.get("LeagueID"))
        if blackout_league not in {league_id, "*"}:
            continue
        start = as_date(row.get("StartDate"))
        end = as_date(row.get("EndDate"))
        if start and end and start <= check_date <= end:
            reason = clean_str(row.get("Reason")) or clean_str(row.get("Type")) or "Blackout"
            return True, reason
    return False, ""


def get_active_leagues(leagues: pd.DataFrame) -> pd.DataFrame:
    if leagues.empty:
        return leagues
    df = leagues.copy()
    df["LeagueID"] = df["LeagueID"].map(clean_str)
    df["Status"] = df["Status"].map(clean_str)
    return df[df["LeagueID"].ne("") & ~df["Status"].str.lower().isin(["inactive", "closed", "done"])]


def build_league_schedule(
    league: pd.Series,
    teams_df: pd.DataFrame,
    slots_df: pd.DataFrame,
    blackouts_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    league_id = clean_str(league.get("LeagueID"))
    league_name = clean_str(league.get("LeagueName")) or league_id
    start = as_date(league.get("StartDate"))
    weeks = int(pd.to_numeric(league.get("Weeks"), errors="coerce") or 0)
    games_per_night = int(pd.to_numeric(league.get("GamesPerNight"), errors="coerce") or 0)

    audit_rows: List[Dict[str, Any]] = []
    schedule_rows: List[Dict[str, Any]] = []

    if not start:
        audit_rows.append(audit(league_id, league_name, "Error", None, None, None, "Missing start date", "League cannot be scheduled without StartDate."))
        return pd.DataFrame(columns=SCHEDULE_COLUMNS), pd.DataFrame(audit_rows, columns=AUDIT_COLUMNS)
    if weeks <= 0:
        audit_rows.append(audit(league_id, league_name, "Error", None, None, None, "Invalid Weeks", "Weeks must be greater than 0."))
        return pd.DataFrame(columns=SCHEDULE_COLUMNS), pd.DataFrame(audit_rows, columns=AUDIT_COLUMNS)

    teams = teams_df[teams_df["LeagueID"].map(clean_str) == league_id].copy()
    teams = teams[teams["Active"].map(truthy)]
    team_map = {clean_str(r.TeamID): clean_str(r.TeamName) for r in teams.itertuples(index=False)}
    team_ids = [tid for tid in team_map if tid]

    if len(team_ids) < 2:
        audit_rows.append(audit(league_id, league_name, "Error", None, None, None, "Not enough teams", "At least 2 active teams are required."))
        return pd.DataFrame(columns=SCHEDULE_COLUMNS), pd.DataFrame(audit_rows, columns=AUDIT_COLUMNS)

    slots = slots_df[slots_df["LeagueID"].map(clean_str) == league_id].copy()
    if slots.empty:
        audit_rows.append(audit(league_id, league_name, "Error", None, None, None, "Missing time slots", "No TimeSlots rows found for this LeagueID."))
        return pd.DataFrame(columns=SCHEDULE_COLUMNS), pd.DataFrame(audit_rows, columns=AUDIT_COLUMNS)

    slots["SlotOrder"] = pd.to_numeric(slots["SlotOrder"], errors="coerce").fillna(999).astype(int)
    slots["Time"] = slots["Time"].map(as_time_label)
    slots = slots.sort_values("SlotOrder")
    if games_per_night <= 0:
        games_per_night = len(slots)
    slots = slots.head(games_per_night)

    total_slots = weeks * len(slots)
    rounds = round_robin_pairs(team_ids)
    flattened: List[Tuple[str, str]] = []
    while len(flattened) < total_slots:
        for rr_round in rounds:
            flattened.extend(rr_round)
            if len(flattened) >= total_slots:
                break

    if len(team_ids) % 2 == 1:
        audit_rows.append(audit(league_id, league_name, "Info", None, None, None, "Odd team count", "A bye is expected each round because this league has an odd number of teams."))

    game_index = 0
    week_date = start
    for week in range(1, weeks + 1):
        pushed = 0
        while True:
            blocked, reason = is_blackout(week_date, blackouts_df, league_id)
            if not blocked:
                break
            pushed += 1
            audit_rows.append(audit(league_id, league_name, "Warning", week, week_date, None, "Blackout date pushed", f"{week_date.isoformat()} skipped: {reason}"))
            week_date = week_date + timedelta(days=7)
            if pushed > 20:
                audit_rows.append(audit(league_id, league_name, "Error", week, week_date, None, "Too many blackout pushes", "Stopped after 20 pushes to prevent an infinite loop."))
                break

        for _, slot in slots.iterrows():
            if game_index >= len(flattened):
                break
            home, away = flattened[game_index]
            schedule_rows.append({
                "LeagueID": league_id,
                "LeagueName": league_name,
                "Week": week,
                "Date": week_date.isoformat(),
                "Time": slot["Time"],
                "SlotOrder": int(slot["SlotOrder"]),
                "HomeTeamID": home,
                "HomeTeam": team_map.get(home, home),
                "AwayTeamID": away,
                "AwayTeam": team_map.get(away, away),
                "Notes": "",
                "Flag": "",
                "FlagReason": "",
            })
            game_index += 1
        week_date = week_date + timedelta(days=7)

    schedule = pd.DataFrame(schedule_rows, columns=SCHEDULE_COLUMNS)
    audit_rows.extend(audit_schedule(schedule, team_ids, team_map, league_id, league_name))
    return schedule, pd.DataFrame(audit_rows, columns=AUDIT_COLUMNS)


def audit(league_id, league_name, severity, week, day, time, issue, details, row_ref="") -> Dict[str, Any]:
    return {
        "LeagueID": league_id,
        "LeagueName": league_name,
        "Severity": severity,
        "Week": week or "",
        "Date": day.isoformat() if isinstance(day, date) else (day or ""),
        "Time": time or "",
        "Issue": issue,
        "Details": details,
        "RowRef": row_ref,
    }


def audit_schedule(schedule: pd.DataFrame, team_ids: List[str], team_map: Dict[str, str], league_id: str, league_name: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if schedule.empty:
        return rows

    counts = {tid: 0 for tid in team_ids}
    home_counts = {tid: 0 for tid in team_ids}
    late_counts = {tid: 0 for tid in team_ids}
    max_slot = schedule["SlotOrder"].max()

    for i, game in schedule.iterrows():
        home = clean_str(game["HomeTeamID"])
        away = clean_str(game["AwayTeamID"])
        for tid in [home, away]:
            counts[tid] = counts.get(tid, 0) + 1
            if int(game["SlotOrder"]) == int(max_slot):
                late_counts[tid] = late_counts.get(tid, 0) + 1
        home_counts[home] = home_counts.get(home, 0) + 1
        if home == away:
            rows.append(audit(league_id, league_name, "Error", game["Week"], game["Date"], game["Time"], "Team scheduled against itself", home, f"Schedule row {i+2}"))

    game_values = list(counts.values())
    if game_values and max(game_values) - min(game_values) > 1:
        rows.append(audit(league_id, league_name, "Warning", None, None, None, "Uneven games per team", str(counts)))

    for tid in team_ids:
        away_count = counts[tid] - home_counts.get(tid, 0)
        if abs(home_counts.get(tid, 0) - away_count) > 2:
            rows.append(audit(league_id, league_name, "Warning", None, None, None, "Home/away imbalance", f"{team_map.get(tid, tid)}: home {home_counts.get(tid, 0)}, away {away_count}"))
        if late_counts.get(tid, 0) > max(1, round(counts[tid] * 0.6)):
            rows.append(audit(league_id, league_name, "Info", None, None, None, "Late slot concentration", f"{team_map.get(tid, tid)} has {late_counts.get(tid, 0)} late games out of {counts[tid]}."))

    return rows


def build_all_schedules(tables: Dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    tables = normalize_tables(tables)
    all_schedule: List[pd.DataFrame] = []
    all_audit: List[pd.DataFrame] = []

    for _, league in get_active_leagues(tables["Leagues"]).iterrows():
        schedule, audit_df = build_league_schedule(
            league,
            tables["Teams"],
            tables["TimeSlots"],
            tables["Blackouts"],
        )
        all_schedule.append(schedule)
        all_audit.append(audit_df)

    schedule_out = pd.concat(all_schedule, ignore_index=True) if all_schedule else pd.DataFrame(columns=SCHEDULE_COLUMNS)
    audit_out = pd.concat(all_audit, ignore_index=True) if all_audit else pd.DataFrame(columns=AUDIT_COLUMNS)
    return schedule_out, audit_out
