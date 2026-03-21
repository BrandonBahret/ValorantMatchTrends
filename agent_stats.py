"""
agent_stats.py
──────────────
Per-agent and per-role statistics across a set of matches, plus an HTML
report generator.

All functions accept a ``excluded_puuid`` parameter so the tracked player
is not counted in opponent stats.
"""

import json
from typing import Dict, List

from api_valorant_assets import ValAssetApi
from api_henrik import Match, Player


# ── Agent presence ────────────────────────────────────────────────────────────

def agent_team_percentage(matches: List[Match], agent_name: str) -> float:
    """
    Return the percentage of teams (across all matches) that had at least
    one player on ``agent_name``.

    Parameters
    ----------
    matches : list[Match]
    agent_name : str
        e.g. ``"Viper"``

    Returns
    -------
    float
        Percentage in [0, 100].
    """
    total_teams = agent_teams = 0

    for match in matches:
        teams: Dict[str, list] = {}
        for player in match.players:
            teams.setdefault(player.team_id, []).append(player)

        total_teams += 2
        for team_players in teams.values():
            if any(p.character == agent_name for p in team_players):
                agent_teams += 1

    return (agent_teams / total_teams * 100) if total_teams else 0.0


# ── Comprehensive agent stats ─────────────────────────────────────────────────

def calculate_agent_stats(matches: List[Match], excluded_puuid: str) -> dict:
    """
    Aggregate per-agent stats across all matches, ignoring ``excluded_puuid``.

    Returns a dict keyed by agent name with fields:
      ``picks``, ``teams``, ``wins``, ``kills``, ``deaths``,
      ``assists``, ``matches_seen`` (count of unique matches).
    """
    agent_stats: Dict[str, dict] = {}

    # ── Individual player stats + non-mirror tracking ──────────────────────
    for match in matches:
        winning_team_id = (
            [tid for tid, data in match.teams.as_dict().items() if data["has_won"]] + [None]
        )[0]

        # Build per-team agent sets (excluding the tracked player)
        teams: Dict[str, list] = {}
        excluded_team_id = None
        for player in match.players:
            if player.puuid == excluded_puuid:
                excluded_team_id = player.team_id
            teams.setdefault(player.team_id, []).append(player)

        agents_per_team: Dict[str, set] = {
            tid: {p.character for p in players if p.puuid != excluded_puuid}
            for tid, players in teams.items()
        }

        for player in match.players:
            if player.puuid == excluded_puuid:
                continue

            agent = player.character
            if agent not in agent_stats:
                agent_stats[agent] = {
                    "picks": 0, "teams": 0, "wins": 0,
                    "kills": 0, "deaths": 0, "assists": 0,
                    "matches_seen": set(),
                    "non_mirror_picks": 0, "non_mirror_wins": 0,
                }

            s = agent_stats[agent]
            s["picks"]   += 1
            s["kills"]   += player.stats.kills
            s["deaths"]  += player.stats.deaths
            s["assists"] += player.stats.assists
            s["matches_seen"].add(match.metadata.match_id)

            won = player.team_id.lower() == winning_team_id
            if won:
                s["wins"] += 1

            # Non-mirror: agent is on this team but NOT on the opposing team
            opposing_agents = set()
            for tid, agent_set in agents_per_team.items():
                if tid != player.team_id:
                    opposing_agents |= agent_set

            if agent not in opposing_agents:
                s["non_mirror_picks"] += 1
                if won:
                    s["non_mirror_wins"] += 1

    # ── Team-level presence ────────────────────────────────────────────────
    for match in matches:
        teams: Dict[str, list] = {}
        for player in match.players:
            teams.setdefault(player.team_id, []).append(player)

        for team_players in teams.values():
            agents_on_team = {
                p.character for p in team_players if p.puuid != excluded_puuid
            }
            for agent in agents_on_team:
                if agent in agent_stats:
                    agent_stats[agent]["teams"] += 1

    # Convert matches_seen sets to counts
    for agent in agent_stats:
        agent_stats[agent]["matches_seen"] = len(agent_stats[agent]["matches_seen"])

    return agent_stats


# ── Role percentages ──────────────────────────────────────────────────────────

def calculate_role_percentages(
    matches: List[Match], excluded_puuid: str
) -> Dict[str, str]:
    """
    Calculate the percentage of *opponent* team compositions that included
    each role, ignoring the excluded player's team.

    Returns a dict of ``role_name → "X.X%"`` strings.
    """
    agent_api = ValAssetApi()
    role_counts: Dict[str, int] = {}
    total_teams = 0

    for match in matches:
        teams: Dict[str, list] = {}
        excluded_team = None

        for player in match.players:
            if player.puuid == excluded_puuid:
                excluded_team = player.team_id
            teams.setdefault(player.team_id, []).append(player)

        for team_id, team_players in teams.items():
            if team_id == excluded_team:
                continue

            total_teams += 1
            roles_in_team = set()
            for player in team_players:
                role = agent_api.agents[player.character].role.displayName
                roles_in_team.add(role)

            for role in roles_in_team:
                role_counts[role] = role_counts.get(role, 0) + 1

    return {
        role: f"{count / total_teams:.1%}"
        for role, count in role_counts.items()
    }


# ── HTML report ───────────────────────────────────────────────────────────────

def generate_html_report(
    matches: List[Match],
    excluded_puuid: str,
    output_file: str = "index.html",
) -> None:
    """
    Write an interactive, sortable HTML agent-statistics page to *output_file*.

    Parameters
    ----------
    matches : list[Match]
    excluded_puuid : str
        The tracked player's PUUID (excluded from stats).
    output_file : str
        Path to write the HTML file, default ``"index.html"``.
    """
    assets = ValAssetApi()
    stats  = calculate_agent_stats(matches, excluded_puuid)
    representation_stats = calculate_role_percentages(matches, excluded_puuid)

    total_teams     = len(matches) * 2
    agent_data_list = []

    for agent_name, agent_data in stats.items():
        agent_info = assets.agents.get(agent_name)
        if agent_info:
            icon = agent_info.displayIconSmall or agent_info.displayIcon
            role = agent_info.role.displayName if agent_info.role else "Unknown"
        else:
            icon, role = "", "Unknown"

        picks            = agent_data["picks"]
        teams            = agent_data["teams"]
        matches_seen     = agent_data["matches_seen"]
        kills, deaths, assists = agent_data["kills"], agent_data["deaths"], agent_data["assists"]

        nm_picks = agent_data["non_mirror_picks"]
        agent_data_list.append({
            "name":                    agent_name,
            "icon":                    icon,
            "role":                    role,
            "teams":                   teams,
            "team_percentage":         teams / total_teams * 100 if total_teams else 0,
            "picks":                   picks,
            "matches_seen":            matches_seen,
            "matches_seen_percentage": matches_seen / len(matches) * 100 if matches else 0,
            "win_rate":                agent_data["wins"] / picks * 100 if picks else 0,
            "non_mirror_win_rate":     agent_data["non_mirror_wins"] / nm_picks * 100 if nm_picks else None,
            "non_mirror_picks":        nm_picks,
            "kda":                     (kills + assists) / deaths if deaths else kills + assists,
        })

    unique_agents   = len(agent_data_list)
    agents_json     = json.dumps(agent_data_list)
    repr_json       = json.dumps(representation_stats)

    html_content = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Valorant Agent Statistics</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f1923 0%, #1a2733 100%);
            color: #ece8e1;
            min-height: 100vh;
            padding: 20px;
        }}

        .container {{
            max-width: 1600px;
            margin: 0 auto;
        }}

        header {{
            text-align: center;
            margin-bottom: 30px;
            padding: 30px 0;
            border-bottom: 2px solid #ff4655;
        }}

        h1 {{
            font-size: 3em;
            color: #ff4655;
            text-transform: uppercase;
            letter-spacing: 3px;
            margin-bottom: 10px;
            text-shadow: 0 0 20px rgba(255, 70, 85, 0.5);
        }}

        .summary {{
            text-align: center;
            font-size: 1.2em;
            color: #b0aca7;
            margin-top: 10px;
        }}

        .controls {{
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 30px;
            display: flex;
            gap: 20px;
            flex-wrap: wrap;
            align-items: center;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}

        .control-group {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .control-group label {{
            color: #b0aca7;
            font-weight: 500;
        }}

        input[type="number"] {{
            background: #f9f9f9;
            color: #333;
            border: 1px solid rgba(0, 0, 0, 0.2);
            padding: 10px 15px;
            border-radius: 5px;
            font-size: 1em;
            cursor: text;
            transition: all 0.3s ease;
        }}

        input[type="number"]:hover,
        input[type="number"]:focus {{
            border-color: rgba(0, 0, 0, 0.4);
            outline: none;
        }}

        select, button {{
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #fff;
            padding: 10px 15px;
            border-radius: 5px;
            font-size: 1em;
            cursor: pointer;
            transition: all 0.3s ease;
        }}

        select:hover, button:hover {{
            background: rgba(255, 70, 85, 0.3);
            border-color: #ff4655;
        }}

        select:focus, button:focus {{
            outline: none;
            border-color: #ff4655;
        }}

        select option {{
            background-color: white;
            color: #333;
        }}

        .checkbox-group {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            cursor: pointer;
        }}

        .stats-table {{
            width: 100%;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 10px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}

        .stats-table thead {{
            position: sticky;
            top: 0;
            z-index: 10;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        thead {{
            background: #4a2d37;
        }}

        th {{
            padding: 15px;
            text-align: left;
            font-weight: 600;
            color: #fff;
            text-transform: uppercase;
            font-size: 0.9em;
            letter-spacing: 1px;
        }}

        th.sortable {{
            cursor: pointer;
            user-select: none;
        }}

        th.sortable:hover {{
            background: rgba(255, 70, 85, 0.3);
        }}

        th.sorted {{
            color: #ff4655;
        }}

        tbody tr {{
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            transition: all 0.2s ease;
        }}

        tbody tr:hover {{
            background: rgba(255, 255, 255, 0.05);
        }}

        td {{
            padding: 15px;
            color: #ece8e1;
        }}

        .agent-cell {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .agent-icon {{
            width: 40px;
            height: 40px;
            border-radius: 50%;
            border: 2px solid #ff4655;
            object-fit: cover;
        }}

        .agent-info {{
            display: flex;
            flex-direction: column;
        }}

        .agent-name {{
            font-weight: bold;
            font-size: 1.1em;
            color: #fff;
        }}

        .agent-role {{
            font-size: 0.85em;
            color: #ff4655;
            text-transform: uppercase;
        }}

        .stat-number {{
            font-weight: 600;
            font-size: 1.05em;
        }}

        .percentage {{
            color: #00d4aa;
        }}

        .kda {{
            color: #ffd966;
        }}

        .role-header {{
            background: rgba(255, 70, 85, 0.15);
            padding: 12px 15px;
            font-weight: bold;
            font-size: 1.1em;
            color: #ff4655;
            text-transform: uppercase;
            letter-spacing: 1px;
        }}

        .role-section {{
            border-top: 2px solid #ff4655;
        }}
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>Agent Statistics</h1>
        <div class="summary">
            Total Matches: {len(matches)} | Total Teams: {total_teams} | Unique Agents: {unique_agents}
        </div>
    </header>

    <div class="controls">
        <div class="control-group">
            <label for="sortBy">Sort by:</label>
            <select id="sortBy">
                <option value="team_percentage">Team Pick Rate</option>
                <option value="matches_seen_percentage">Matches Seen %</option>
                <option value="win_rate">Win Rate</option>
                <option value="non_mirror_win_rate">Non-Mirror Win Rate</option>
                <option value="kda">KDA Ratio</option>
                <option value="name">Agent Name</option>
            </select>
        </div>

        <div class="control-group">
            <label for="sortOrder">Order:</label>
            <select id="sortOrder">
                <option value="desc">Descending</option>
                <option value="asc">Ascending</option>
            </select>
        </div>

        <div class="control-group">
            <label for="minMatches">Minimum Matches:</label>
            <input type="number" id="minMatches" min="0" value="10" step="1">
        </div>

        <div class="checkbox-group">
            <input type="checkbox" id="groupByRole">
            <label for="groupByRole">Group by Role</label>
        </div>
    </div>

    <div class="stats-table">
        <table>
            <thead>
                <tr>
                    <th>Agent</th>
                    <th class="sortable" data-sort="teams">Team Appearances</th>
                    <th class="sortable" data-sort="team_percentage">Team Pick Rate</th>
                    <th class="sortable" data-sort="matches_seen_percentage">Matches Seen %</th>
                    <th class="sortable" data-sort="win_rate">Win Rate</th>
                    <th class="sortable" data-sort="non_mirror_win_rate">Non-Mirror Win Rate</th>
                    <th class="sortable" data-sort="kda">KDA Ratio</th>
                </tr>
            </thead>
            <tbody id="agentTableBody"></tbody>
        </table>
    </div>
</div>

<script>
    const agentsData = {agents_json};
    const reprData   = {repr_json};
    let currentSort  = 'team_percentage';
    let currentOrder = 'desc';
    let groupByRole  = false;

    function renderTable() {{
        const tbody      = document.getElementById('agentTableBody');
        tbody.innerHTML  = '';

        const minMatches = Number(document.getElementById('minMatches').value);

        let sortedAgents = agentsData.filter(agent => agent.matches_seen >= minMatches);

        sortedAgents.sort((a, b) => {{
            let valA = a[currentSort];
            let valB = b[currentSort];
            // Nulls always sort to the bottom regardless of direction
            if (valA === null && valB === null) return 0;
            if (valA === null) return 1;
            if (valB === null) return -1;
            if (typeof valA === 'string') {{
                valA = valA.toLowerCase();
                valB = valB.toLowerCase();
            }}
            return currentOrder === 'asc'
                ? (valA > valB ? 1 : -1)
                : (valA < valB ? 1 : -1);
        }});

        if (groupByRole) {{
            const grouped = {{}};
            sortedAgents.forEach(agent => {{
                if (!grouped[agent.role]) grouped[agent.role] = [];
                grouped[agent.role].push(agent);
            }});

            Object.keys(grouped).sort().forEach(role => {{
                const headerRow       = document.createElement('tr');
                headerRow.className   = 'role-section';
                const playRate        = reprData[role] || 'N/A';
                headerRow.innerHTML   = `<td colspan="7" class="role-header">${{role}} (play rate ${{playRate}})</td>`;
                tbody.appendChild(headerRow);
                grouped[role].forEach(agent => tbody.appendChild(createAgentRow(agent)));
            }});
        }} else {{
            sortedAgents.forEach(agent => tbody.appendChild(createAgentRow(agent)));
        }}

        // Highlight active sort column header
        document.querySelectorAll('th.sortable').forEach(th => th.classList.remove('sorted'));
        const sortedHeader = document.querySelector(`th[data-sort="${{currentSort}}"]`);
        if (sortedHeader) sortedHeader.classList.add('sorted');

        // Win rate colour coding
        document.querySelectorAll('.winrate-filter').forEach(el => {{
            const value = parseFloat(el.textContent.replace('%', ''));
            el.style.color      = '#d40055';
            el.style.textShadow = 'none';

            if (value >= 55) {{
                el.style.color      = '#00d4aa';
                el.style.textShadow = '0 0 8px #00d4aa';
            }} else if (value >= 50.5) {{
                el.style.color      = '#00d4aa';
                el.style.textShadow = 'none';
            }} else if (value >= 45) {{
                el.style.color      = '#fff';
                el.style.textShadow = 'none';
            }}
        }});
    }}

    function createAgentRow(agent) {{
        const row = document.createElement('tr');
        row.innerHTML = `
            <td>
                <div class="agent-cell">
                    <img src="${{agent.icon}}" alt="${{agent.name}}" class="agent-icon"
                         onerror="this.style.display='none'">
                    <div class="agent-info">
                        <div class="agent-name">${{agent.name}}</div>
                        <div class="agent-role">${{agent.role}}</div>
                    </div>
                </div>
            </td>
            <td class="stat-number">${{agent.teams}}</td>
            <td class="stat-number percentage">${{agent.team_percentage.toFixed(1)}}%</td>
            <td class="stat-number">
                <span class="percentage">${{agent.matches_seen_percentage.toFixed(1)}}%</span>
                (<span class="stat-number">${{agent.matches_seen}}</span>)
            </td>
            <td class="stat-number winrate-filter percentage">${{agent.win_rate.toFixed(1)}}%</td>
            <td class="stat-number winrate-filter percentage">
                ${{agent.non_mirror_win_rate !== null
                    ? agent.non_mirror_win_rate.toFixed(1) + '%  <span style="font-size:0.8em;opacity:0.6;">(' + agent.non_mirror_picks + ')</span>'
                    : '<span style="opacity:0.4;">—</span>'}}
            </td>
            <td class="stat-number kda">${{agent.kda.toFixed(2)}}</td>
        `;
        return row;
    }}

    // Controls
    document.getElementById('sortBy').addEventListener('change', e => {{
        currentSort = e.target.value;
        renderTable();
    }});

    document.getElementById('sortOrder').addEventListener('change', e => {{
        currentOrder = e.target.value;
        renderTable();
    }});

    document.getElementById('groupByRole').addEventListener('change', e => {{
        groupByRole = e.target.checked;
        renderTable();
    }});

    document.getElementById('minMatches').addEventListener('input', () => renderTable());

    // Clicking column headers also sorts
    document.querySelectorAll('th.sortable').forEach(th => {{
        th.addEventListener('click', () => {{
            const sortKey = th.dataset.sort;
            if (currentSort === sortKey) {{
                currentOrder = currentOrder === 'desc' ? 'asc' : 'desc';
            }} else {{
                currentSort  = sortKey;
                currentOrder = 'desc';
            }}
            document.getElementById('sortBy').value    = currentSort;
            document.getElementById('sortOrder').value = currentOrder;
            renderTable();
        }});
    }});

    // Initial render
    renderTable();
</script>
</body>
</html>'''

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Report written to {output_file}")