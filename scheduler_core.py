from __future__ import annotations

from datetime import datetime, timedelta, date
from itertools import combinations
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

SUMMARY_COLUMNS = [
    "LeagueID", "LeagueName", "TeamID", "TeamName",
    "Games", "Home", "Away", "Byes", "Doubleheaders", "LateGames",
    "HomeAwayDiff", "TargetGamesDiff", "TimeSlotImbalance"
]

# MVP scheduling preferences.
# Lower score = better option.
SCHEDULE_WEIGHTS = {
    # Strongest priority: keep total games tight.
    "balance_games": 240,
    # Doubleheaders are allowed, but should be spread around.
    "balance_doubleheaders": 140,
    "doubleheader_penalty": 30,
    # Home/away still matters, but not more than total games.
    "balance_home_away": 45,
    # Balance all time slots, not just the latest slot.
    "balance_time_slots": 70,
    # Last slot still gets a small extra penalty because late games usually feel worse.
    "extra_late_slot": 20,
    # Repeat-opponent spacing.
    "avoid_repeat_opponent": 45,
    "avoid_recent_repeat": 130,
}

MAX_GAMES_PER_TEAM_PER_WEEK = 2
DOUBLEHEADERS_MUST_BE_BACK_TO_BACK = True

# If time slots are provided, the scheduler treats them as intended-use slots.
# Weekly capacity is slots * 2 team appearances. Example: 7 teams + 4 slots
# means 8 appearances, so everyone plays and one team gets a doubleheader.
USE_ALL_LISTED_SLOTS_WHEN_POSSIBLE = True



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


def pair_key(team_a: str, team_b: str) -> tuple[str, str]:
    return tuple(sorted((team_a, team_b)))


def choose_home_away(team_a: str, team_b: str, stats: dict) -> tuple[str, str]:
    """Choose home team based on current home/away imbalance."""
    def orientation_penalty(home: str, away: str) -> int:
        home_after = stats["home"][home] + 1
        away_after = stats["games"][home] + 1 - home_after
        home_diff_home = abs(home_after - away_after)

        away_home_after = stats["home"][away]
        away_away_after = stats["games"][away] + 1 - away_home_after
        away_diff = abs(away_home_after - away_away_after)

        return home_diff_home + away_diff

    if orientation_penalty(team_a, team_b) <= orientation_penalty(team_b, team_a):
        return team_a, team_b
    return team_b, team_a


def team_time_slot_imbalance(team: str, stats: dict) -> int:
    """Return the spread between a team's most-used and least-used time slot."""
    slot_counts = stats.get("slot_counts", {}).get(team, {})
    if not slot_counts:
        return 0
    values = list(slot_counts.values())
    return max(values) - min(values)


def team_slot_count(team: str, slot_order: int, stats: dict) -> int:
    return stats.get("slot_counts", {}).get(team, {}).get(int(slot_order), 0)


def calculate_week_targets(
    team_ids: List[str],
    slot_count: int,
    stats: dict,
) -> dict[str, int]:
    """Return how many times each team should play this week.

    This is capacity-based, not odd/even based.

    Examples:
    - 8 teams / 4 slots = everyone plays once
    - 7 teams / 4 slots = everyone plays once, one team plays twice
    - 9 teams / 4 slots = 8 teams play once, one team has a bye
    """
    capacity = slot_count * 2
    team_count = len(team_ids)
    targets = {team: 0 for team in team_ids}

    if team_count == 0 or slot_count == 0:
        return targets

    if capacity >= team_count:
        # Everyone should play once before anyone is asked to doubleheader.
        for team in team_ids:
            targets[team] = 1

        extra_appearances = min(capacity - team_count, team_count)
        if extra_appearances > 0:
            # Give extra appearances to teams that need games and have had fewer doubleheaders.
            extra_candidates = sorted(
                team_ids,
                key=lambda t: (
                    # First give extra games to teams behind in games.
                    stats["games"].get(t, 0),
                    # Then spread doubleheaders fairly.
                    stats["doubleheaders"].get(t, 0),
                    # Then avoid teams already stuck in a lopsided time-slot pattern.
                    team_time_slot_imbalance(t, stats),
                    t,
                ),
            )
            for team in extra_candidates[:extra_appearances]:
                targets[team] = 2
    else:
        # Not enough slots for everyone. Sit the teams that can best absorb a bye.
        play_count = capacity
        play_candidates = sorted(
            team_ids,
            key=lambda t: (
                stats["games"].get(t, 0),
                -stats["byes"].get(t, 0),
                team_time_slot_imbalance(t, stats),
                t,
            ),
        )
        for team in play_candidates[:play_count]:
            targets[team] = 1

    return targets


def valid_pair_for_slot(
    team_a: str,
    team_b: str,
    slot_index: int,
    week_counts: dict[str, int],
    week_last_slot: dict[str, int],
    week_pairs: set[tuple[str, str]],
    week_targets: dict[str, int],
) -> bool:
    if team_a == team_b:
        return False
    if pair_key(team_a, team_b) in week_pairs:
        return False

    for team in (team_a, team_b):
        if week_counts.get(team, 0) >= MAX_GAMES_PER_TEAM_PER_WEEK:
            return False
        if week_counts.get(team, 0) >= week_targets.get(team, 0):
            return False

        # If this would be the team's second game this week, require it to be
        # immediately after their first game. Nobody wants to wait for game two.
        if week_counts.get(team, 0) == 1 and DOUBLEHEADERS_MUST_BE_BACK_TO_BACK:
            if week_last_slot.get(team) != slot_index - 1:
                return False

    return True


def score_pair(
    team_a: str,
    team_b: str,
    slot_order: int,
    max_slot_order: int,
    current_week: int,
    stats: dict,
    week_counts: dict[str, int],
    week_targets: dict[str, int],
) -> float:
    teams = (team_a, team_b)
    min_games = min(stats["games"].values()) if stats["games"] else 0
    max_games = max(stats["games"].values()) if stats["games"] else 0
    min_doubleheaders = min(stats["doubleheaders"].values()) if stats["doubleheaders"] else 0
    score = 0.0

    # Capacity target pressure: teams that still need appearances this week are preferred.
    remaining_targets = {}
    for team in teams:
        remaining_target = week_targets.get(team, 0) - week_counts.get(team, 0)
        remaining_targets[team] = remaining_target
        score -= remaining_target * 120

    # Avoid starting two doubleheader target teams in the same game when possible;
    # it can make the next slot impossible without repeating the same matchup.
    if all(week_targets.get(team, 0) >= 2 and week_counts.get(team, 0) == 0 for team in teams):
        score += 500

    # Prefer teams with fewer total games across the season. This is intentionally
    # heavy: a pretty schedule is not useful if the game counts drift.
    for team in teams:
        current_games = stats["games"][team]
        score += (current_games - min_games) * SCHEDULE_WEIGHTS["balance_games"]

        # Extra protection: strongly avoid giving more games to teams that are
        # already ahead of the league minimum.
        projected_week_games = week_counts.get(team, 0) + 1
        if current_games > min_games and projected_week_games > 0:
            score += (current_games - min_games) * 180
        if max_games - min_games >= 1 and current_games == max_games:
            score += 160

    # Controlled doubleheaders are allowed when the slot capacity calls for them,
    # but spread them fairly across the season.
    for team in teams:
        if week_counts.get(team, 0) == 1:
            score += SCHEDULE_WEIGHTS["doubleheader_penalty"]
            score += (stats["doubleheaders"].get(team, 0) - min_doubleheaders) * SCHEDULE_WEIGHTS["balance_doubleheaders"]
            score += stats["doubleheaders"].get(team, 0) * 25
            # If this team was selected as a weekly doubleheader target, reduce penalty.
            if week_targets.get(team, 0) >= 2:
                score -= 120
            # If a team has fewer games overall, doubleheader is less bad.
            score -= max(0, min_games + 1 - stats["games"][team]) * 70

    # Balance every time slot. If this is Slot 2, prefer teams that have
    # had fewer Slot 2 games. This prevents teams from getting stuck in a
    # repeated time pattern, not just too many late games.
    min_this_slot = min(team_slot_count(t, slot_order, stats) for t in stats["games"]) if stats.get("games") else 0
    for team in teams:
        this_slot_count = team_slot_count(team, slot_order, stats)
        score += (this_slot_count - min_this_slot) * SCHEDULE_WEIGHTS["balance_time_slots"]
        # Also penalize the projected per-team slot spread after this game.
        projected_counts = dict(stats.get("slot_counts", {}).get(team, {}))
        projected_counts[int(slot_order)] = projected_counts.get(int(slot_order), 0) + 1
        if projected_counts:
            score += (max(projected_counts.values()) - min(projected_counts.values())) * 35

    # Keep a smaller extra penalty for the last slot because it usually feels
    # worse than other times, but it is no longer the only slot being balanced.
    if int(slot_order) == int(max_slot_order):
        min_late = min(stats["late"].values()) if stats["late"] else 0
        for team in teams:
            score += (stats["late"][team] - min_late) * SCHEDULE_WEIGHTS["extra_late_slot"]
            if stats["games"][team] > min_games:
                score += 35

    # Avoid repeat opponents and especially recent repeat opponents.
    pk = pair_key(team_a, team_b)
    score += stats["opponent_counts"].get(pk, 0) * SCHEDULE_WEIGHTS["avoid_repeat_opponent"]

    last_met = stats["last_met_week"].get(pk)
    if last_met is not None:
        gap = current_week - last_met
        if gap <= 1:
            score += SCHEDULE_WEIGHTS["avoid_recent_repeat"] * 2
        elif gap == 2:
            score += SCHEDULE_WEIGHTS["avoid_recent_repeat"]

    # Choose the pairing that will likely create the smallest home/away imbalance.
    home, away = choose_home_away(team_a, team_b, stats)
    home_after = stats["home"][home] + 1
    home_away_after = stats["games"][home] + 1 - home_after
    away_home_after = stats["home"][away]
    away_away_after = stats["games"][away] + 1 - away_home_after
    score += (abs(home_after - home_away_after) + abs(away_home_after - away_away_after)) * SCHEDULE_WEIGHTS["balance_home_away"]

    return score


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

    max_slot_order = int(slots["SlotOrder"].max())
    slot_orders = [int(x) for x in slots["SlotOrder"].tolist()]

    stats = {
        "games": {team: 0 for team in team_ids},
        "home": {team: 0 for team in team_ids},
        "late": {team: 0 for team in team_ids},
        "slot_counts": {team: {slot_order: 0 for slot_order in slot_orders} for team in team_ids},
        "byes": {team: 0 for team in team_ids},
        "doubleheaders": {team: 0 for team in team_ids},
        "opponent_counts": {},
        "last_met_week": {},
    }

    all_pairs = list(combinations(team_ids, 2))
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

        week_counts = {team: 0 for team in team_ids}
        week_last_slot: dict[str, int] = {}
        week_pairs: set[tuple[str, str]] = set()
        week_targets = calculate_week_targets(team_ids, len(slots), stats)

        for slot_index, (_, slot) in enumerate(slots.iterrows(), start=1):
            candidates = []
            for team_a, team_b in all_pairs:
                if not valid_pair_for_slot(team_a, team_b, slot_index, week_counts, week_last_slot, week_pairs, week_targets):
                    continue
                candidate_score = score_pair(
                    team_a,
                    team_b,
                    int(slot["SlotOrder"]),
                    max_slot_order,
                    week,
                    stats,
                    week_counts,
                    week_targets,
                )
                candidates.append((candidate_score, team_a, team_b))

            urgent_teams = [
                team for team in team_ids
                if week_targets.get(team, 0) >= 2
                and week_counts.get(team, 0) == 1
                and week_last_slot.get(team) == slot_index - 1
            ]
            if urgent_teams:
                filtered = [
                    item for item in candidates
                    if item[1] in urgent_teams or item[2] in urgent_teams
                ]
                if filtered:
                    candidates = filtered
            else:
                # If a team needs a doubleheader and has not started it yet, try to start
                # that back-to-back pair while there is still a following slot available.
                setup_teams = [
                    team for team in team_ids
                    if week_targets.get(team, 0) >= 2
                    and week_counts.get(team, 0) == 0
                    and slot_index < len(slots)
                ]
                if setup_teams:
                    # Prefer exactly one setup team in the matchup. Pairing two
                    # doubleheader-target teams together can make it impossible
                    # to finish both back-to-back runs in the next slot.
                    exact_one_setup = [
                        item for item in candidates
                        if ((item[1] in setup_teams) ^ (item[2] in setup_teams))
                    ]
                    if exact_one_setup:
                        candidates = exact_one_setup
                    else:
                        filtered = [
                            item for item in candidates
                            if item[1] in setup_teams or item[2] in setup_teams
                        ]
                        if filtered:
                            candidates = filtered

            if not candidates:
                audit_rows.append(audit(
                    league_id,
                    league_name,
                    "Error",
                    week,
                    week_date,
                    slot["Time"],
                    "Unable to fill game slot",
                    "No valid matchup found without violating hard rules.",
                ))
                schedule_rows.append({
                    "LeagueID": league_id,
                    "LeagueName": league_name,
                    "Week": week,
                    "Date": week_date.isoformat(),
                    "Time": slot["Time"],
                    "SlotOrder": int(slot["SlotOrder"]),
                    "HomeTeamID": "",
                    "HomeTeam": "",
                    "AwayTeamID": "",
                    "AwayTeam": "",
                    "Notes": "",
                    "Flag": "OPEN",
                    "FlagReason": "No valid matchup found",
                })
                continue

            candidates.sort(key=lambda item: item[0])
            _, team_a, team_b = candidates[0]
            home, away = choose_home_away(team_a, team_b, stats)
            pk = pair_key(home, away)

            notes = []
            for team in (home, away):
                if week_counts[team] == 1:
                    stats["doubleheaders"][team] += 1
                    notes.append(f"{team_map.get(team, team)} doubleheader")

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
                "Notes": "; ".join(notes),
                "Flag": "",
                "FlagReason": "",
            })

            for team in (home, away):
                stats["games"][team] += 1
                week_counts[team] += 1
                week_last_slot[team] = slot_index
                slot_order_value = int(slot["SlotOrder"])
                stats["slot_counts"].setdefault(team, {}).setdefault(slot_order_value, 0)
                stats["slot_counts"][team][slot_order_value] += 1
                if slot_order_value == max_slot_order:
                    stats["late"][team] += 1

            stats["home"][home] += 1
            stats["opponent_counts"][pk] = stats["opponent_counts"].get(pk, 0) + 1
            stats["last_met_week"][pk] = week
            week_pairs.add(pk)

        for team, target in week_targets.items():
            if week_counts.get(team, 0) < target:
                audit_rows.append(audit(
                    league_id,
                    league_name,
                    "Warning",
                    week,
                    week_date,
                    None,
                    "Weekly target not met",
                    f"{team_map.get(team, team)} targeted for {target} game(s), scheduled {week_counts.get(team, 0)}.",
                ))

        for team, count in week_counts.items():
            if count == 0:
                stats["byes"][team] += 1

        week_date = week_date + timedelta(days=7)

    schedule = pd.DataFrame(schedule_rows, columns=SCHEDULE_COLUMNS)
    audit_rows.extend(audit_schedule(schedule, team_ids, team_map, league_id, league_name, stats))
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



def build_schedule_summary(
    schedule: pd.DataFrame,
    team_ids: List[str],
    team_map: Dict[str, str],
    league_id: str,
    league_name: str,
) -> pd.DataFrame:
    """Create a team-by-team balance summary for easier schedule review."""
    counts = {tid: 0 for tid in team_ids}
    home_counts = {tid: 0 for tid in team_ids}
    late_counts = {tid: 0 for tid in team_ids}
    slot_orders = sorted(pd.to_numeric(schedule["SlotOrder"], errors="coerce").dropna().astype(int).unique()) if not schedule.empty else []
    slot_counts = {tid: {slot_order: 0 for slot_order in slot_orders} for tid in team_ids}
    byes = {tid: 0 for tid in team_ids}
    doubleheaders = {tid: 0 for tid in team_ids}

    if schedule.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    max_slot = schedule["SlotOrder"].dropna().max()
    weekly_counts: dict[tuple[int, str], list[int]] = {}

    for _, game in schedule.iterrows():
        home = clean_str(game.get("HomeTeamID"))
        away = clean_str(game.get("AwayTeamID"))
        if not home or not away:
            continue

        week_value = pd.to_numeric(game.get("Week"), errors="coerce")
        if pd.isna(week_value):
            continue
        week = int(week_value)

        slot_value = pd.to_numeric(game.get("SlotOrder"), errors="coerce")
        if pd.isna(slot_value):
            continue
        slot_order = int(slot_value)

        for tid in [home, away]:
            if tid not in counts:
                continue
            counts[tid] += 1
            weekly_counts.setdefault((week, tid), []).append(slot_order)
            slot_counts.setdefault(tid, {}).setdefault(slot_order, 0)
            slot_counts[tid][slot_order] += 1
            if not pd.isna(max_slot) and slot_order == int(max_slot):
                late_counts[tid] += 1

        if home in home_counts:
            home_counts[home] += 1

    weeks = sorted(pd.to_numeric(schedule["Week"], errors="coerce").dropna().astype(int).unique())
    for week in weeks:
        for tid in team_ids:
            slot_list = weekly_counts.get((week, tid), [])
            if not slot_list:
                byes[tid] += 1
            if len(slot_list) > 1:
                doubleheaders[tid] += 1

    game_values = list(counts.values())
    target_games = round(sum(game_values) / len(game_values), 2) if game_values else 0

    rows = []
    for tid in team_ids:
        games = counts.get(tid, 0)
        home = home_counts.get(tid, 0)
        away = games - home
        row = {
            "LeagueID": league_id,
            "LeagueName": league_name,
            "TeamID": tid,
            "TeamName": team_map.get(tid, tid),
            "Games": games,
            "Home": home,
            "Away": away,
            "Byes": byes.get(tid, 0),
            "Doubleheaders": doubleheaders.get(tid, 0),
            "LateGames": late_counts.get(tid, 0),
            "HomeAwayDiff": abs(home - away),
            "TargetGamesDiff": round(games - target_games, 2),
        }
        for slot_order in slot_orders:
            row[f"Slot{slot_order}"] = slot_counts.get(tid, {}).get(slot_order, 0)
        slot_values = [slot_counts.get(tid, {}).get(slot_order, 0) for slot_order in slot_orders]
        row["TimeSlotImbalance"] = max(slot_values) - min(slot_values) if slot_values else 0
        rows.append(row)

    dynamic_columns = SUMMARY_COLUMNS[:12] + [f"Slot{slot_order}" for slot_order in slot_orders] + ["TimeSlotImbalance"]
    return pd.DataFrame(rows).reindex(columns=dynamic_columns).sort_values(["LeagueID", "TeamName"]).reset_index(drop=True)


def audit_schedule(
    schedule: pd.DataFrame,
    team_ids: List[str],
    team_map: Dict[str, str],
    league_id: str,
    league_name: str,
    stats: dict | None = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if schedule.empty:
        return rows

    counts = {tid: 0 for tid in team_ids}
    home_counts = {tid: 0 for tid in team_ids}
    late_counts = {tid: 0 for tid in team_ids}
    slot_orders = sorted(pd.to_numeric(schedule["SlotOrder"], errors="coerce").dropna().astype(int).unique())
    slot_counts = {tid: {slot_order: 0 for slot_order in slot_orders} for tid in team_ids}
    byes = {tid: 0 for tid in team_ids}
    doubleheaders = {tid: 0 for tid in team_ids}

    max_slot = schedule["SlotOrder"].dropna().max()

    weekly_counts: dict[tuple[int, str], list[int]] = {}
    for i, game in schedule.iterrows():
        home = clean_str(game["HomeTeamID"])
        away = clean_str(game["AwayTeamID"])
        if not home or not away:
            continue

        for tid in [home, away]:
            counts[tid] = counts.get(tid, 0) + 1
            slot_order = int(game["SlotOrder"])
            weekly_counts.setdefault((int(game["Week"]), tid), []).append(slot_order)
            slot_counts.setdefault(tid, {}).setdefault(slot_order, 0)
            slot_counts[tid][slot_order] += 1
            if slot_order == int(max_slot):
                late_counts[tid] = late_counts.get(tid, 0) + 1

        home_counts[home] = home_counts.get(home, 0) + 1

        if home == away:
            rows.append(audit(league_id, league_name, "Error", game["Week"], game["Date"], game["Time"], "Team scheduled against itself", home, f"Schedule row {i+2}"))

    weeks = sorted(pd.to_numeric(schedule["Week"], errors="coerce").dropna().astype(int).unique())
    for week in weeks:
        for tid in team_ids:
            slot_list = weekly_counts.get((week, tid), [])
            if not slot_list:
                byes[tid] += 1
            if len(slot_list) > 1:
                doubleheaders[tid] += 1
                sorted_slots = sorted(slot_list)
                if len(sorted_slots) != 2 or sorted_slots[1] != sorted_slots[0] + 1:
                    rows.append(audit(
                        league_id,
                        league_name,
                        "Error",
                        week,
                        "",
                        "",
                        "Non-consecutive doubleheader",
                        f"{team_map.get(tid, tid)} has a doubleheader that is not back-to-back.",
                    ))

    # Capacity-based sanity checks: if weekly slots could cover all teams, byes should be rare/unexpected.
    slot_count = len(schedule["SlotOrder"].dropna().unique()) if not schedule.empty else 0
    weekly_capacity = slot_count * 2
    if weekly_capacity >= len(team_ids):
        unexpected_byes = {tid: val for tid, val in byes.items() if val > 0}
        if unexpected_byes:
            rows.append(audit(
                league_id,
                league_name,
                "Warning",
                None,
                None,
                None,
                "Unexpected bye with enough slot capacity",
                format_team_counts(unexpected_byes, team_map),
            ))

    game_values = list(counts.values())
    if game_values and max(game_values) - min(game_values) > 1:
        rows.append(audit(league_id, league_name, "Warning", None, None, None, "Uneven games per team", format_team_counts(counts, team_map)))

    bye_values = list(byes.values())
    if bye_values and max(bye_values) - min(bye_values) > 1:
        rows.append(audit(league_id, league_name, "Warning", None, None, None, "Uneven bye count", format_team_counts(byes, team_map)))

    dh_values = list(doubleheaders.values())
    if dh_values and max(dh_values) - min(dh_values) > 1:
        rows.append(audit(league_id, league_name, "Info", None, None, None, "Uneven doubleheader count", format_team_counts(doubleheaders, team_map)))


    # Time-slot distribution: audit every slot, not only the latest one.
    for tid in team_ids:
        values = [slot_counts.get(tid, {}).get(slot_order, 0) for slot_order in slot_orders]
        if values and max(values) - min(values) > 2:
            slot_detail = ", ".join(f"Slot{slot_order}={slot_counts.get(tid, {}).get(slot_order, 0)}" for slot_order in slot_orders)
            rows.append(audit(
                league_id,
                league_name,
                "Info",
                None,
                None,
                None,
                "Time slot imbalance",
                f"{team_map.get(tid, tid)} has uneven time slot distribution: {slot_detail}",
            ))

    for tid in team_ids:
        away_count = counts[tid] - home_counts.get(tid, 0)
        if abs(home_counts.get(tid, 0) - away_count) > 2:
            rows.append(audit(
                league_id,
                league_name,
                "Warning",
                None,
                None,
                None,
                "Home/away imbalance",
                f"{team_map.get(tid, tid)}: home {home_counts.get(tid, 0)}, away {away_count}",
            ))

        if counts.get(tid, 0) and late_counts.get(tid, 0) > max(1, round(counts[tid] * 0.6)):
            rows.append(audit(
                league_id,
                league_name,
                "Info",
                None,
                None,
                None,
                "Late slot concentration",
                f"{team_map.get(tid, tid)} has {late_counts.get(tid, 0)} late games out of {counts[tid]}.",
            ))

    summary = {
        team_map.get(tid, tid): {
            "games": counts.get(tid, 0),
            "home": home_counts.get(tid, 0),
            "away": counts.get(tid, 0) - home_counts.get(tid, 0),
            "byes": byes.get(tid, 0),
            "doubleheaders": doubleheaders.get(tid, 0),
            "late": late_counts.get(tid, 0),
            "slots": {f"Slot{slot_order}": slot_counts.get(tid, {}).get(slot_order, 0) for slot_order in slot_orders},
        }
        for tid in team_ids
    }
    rows.append(audit(league_id, league_name, "Info", None, None, None, "Schedule balance summary", str(summary)))
    return rows


def format_team_counts(counts: dict[str, int], team_map: dict[str, str]) -> str:
    return ", ".join(f"{team_map.get(team, team)}={value}" for team, value in counts.items())


def build_all_schedules(tables: Dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    tables = normalize_tables(tables)
    all_schedule: List[pd.DataFrame] = []
    all_audit: List[pd.DataFrame] = []
    all_summary: List[pd.DataFrame] = []

    for _, league in get_active_leagues(tables["Leagues"]).iterrows():
        schedule, audit_df = build_league_schedule(
            league,
            tables["Teams"],
            tables["TimeSlots"],
            tables["Blackouts"],
        )
        all_schedule.append(schedule)
        all_audit.append(audit_df)

        league_id = clean_str(league.get("LeagueID"))
        league_name = clean_str(league.get("LeagueName")) or league_id
        teams = tables["Teams"][tables["Teams"]["LeagueID"].map(clean_str) == league_id].copy()
        teams = teams[teams["Active"].map(truthy)]
        team_map = {clean_str(r.TeamID): clean_str(r.TeamName) for r in teams.itertuples(index=False)}
        team_ids = [tid for tid in team_map if tid]
        all_summary.append(build_schedule_summary(schedule, team_ids, team_map, league_id, league_name))

    schedule_out = pd.concat(all_schedule, ignore_index=True) if all_schedule else pd.DataFrame(columns=SCHEDULE_COLUMNS)
    audit_out = pd.concat(all_audit, ignore_index=True) if all_audit else pd.DataFrame(columns=AUDIT_COLUMNS)
    summary_out = pd.concat(all_summary, ignore_index=True) if all_summary else pd.DataFrame(columns=SUMMARY_COLUMNS)
    return schedule_out, audit_out, summary_out
