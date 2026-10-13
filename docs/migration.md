# One Moneyball workflow

The default `moneyball` command now runs the connected league workflow. Use `moneyball --help`, `moneyball demo` and `moneyball assess demo`. The MCP executable remains `moneyball-mcp`.

| Previous entry | Current entry |
| --- | --- |
| `moneyball-agent ...` or `moneyball agent ...` | `moneyball ...` (both aliases still work) |
| `art-of-the-deal ...` | `moneyball ...` (compatibility alias retained) |
| `art-of-the-deal-mcp` | `moneyball-mcp` (compatibility alias retained) |
| Old snapshot `moneyball context --league ALIAS` | `moneyball legacy context --league ALIAS` |
| Old experimental `moneyball lab ...` | `moneyball legacy lab ...` |
| `from art_of_the_deal.service import Service` | `from moneyball.league.service import Service` |
| `ART_OF_DEAL_HOME` | `MONEYBALL_HOME` (old variable remains fallback) |

An explicit old `--data-dir` invocation still selects the legacy CLI. Ambiguous unqualified old commands are not guessed: choose `legacy` when using an earlier guide. Historical Python imports forward to the canonical modules, preserving class identity and existing patch/import callers. There is only one connected service implementation.

## Data locations

Selection order is explicit `--home`, `MONEYBALL_HOME`, `ART_OF_DEAL_HOME`, then default. Existing default `~/.local/share/art-of-the-deal/evidence.sqlite3` is reused if present; otherwise new installations use `~/.local/share/moneyball`. This avoids making an existing user's leagues disappear during a software update. Set `MONEYBALL_HOME` explicitly when you want to choose a different root.

The normal root contains `evidence.sqlite3`, raw content and the observed-data `lab/` warehouse. `MONEYBALL_LAB_DIR` can explicitly reuse a previous warehouse. CLI and MCP must use the same root to share their history.

Earlier Sleeper snapshot databases use a different schema. They remain accessible through `moneyball legacy --data-dir PATH ...`; they are not blindly merged or relabeled as fresh. Inspect before relocating any existing store. Changing an environment variable does not copy data, historical acquisition times or authentication. No existing private data is moved by this consolidation.

## Code organization

- `moneyball/cli.py`: primary command and explicit legacy dispatch.
- `moneyball/league/`: connected service, provider adapters, exact-rule decision operations, player assessments, portable skill and MCP.
- Other `moneyball/` modules: research warehouse, analytics, simulation and earlier draft tooling.
- `art_of_the_deal/`: forwarding modules for existing integrations; no new functionality belongs here.

The next cleanup can move analytical internals behind narrower subpackages without changing the public workflow. This migration does not pretend the experimental lab and connected service have identical model assumptions.
