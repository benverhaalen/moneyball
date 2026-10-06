"""Small observed cross-source identity corrections, never fuzzy matching."""
# Observed 2026 Rams records share exact player GSIS/Sleeper IDs but nflverse
# schedules use LA and Sleeper projections use LAR. Raw labels stay unchanged.
TEAM_ALIASES = {'LAR':'LA'}


def canonical_team(team):
    if team is None or str(team).isdigit() or str(team).strip() in ('','FA','None'):
        return None
    value = str(team).strip().upper()
    return TEAM_ALIASES.get(value,value)
