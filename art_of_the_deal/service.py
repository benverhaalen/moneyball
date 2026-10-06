"""Application operations shared by the CLI and MCP. No model API calls."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import math
import os
from urllib.parse import urlparse, parse_qs
from .store import Store, DataError, digest, timestamp
from .http import Client
from .auth import espn_headers
from .strategy import strategy_profile
from . import league_research
from . import sleeper


def alias_ok(alias):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", alias):
        raise DataError("Use a short alphanumeric league alias")
    return alias


class Service:
    def __init__(self, home=None):
        self.store = Store(home)

    def discover_sleeper(self, username, season):
        """Discover memberships without automatically connecting or choosing a team."""
        self._season(season)
        return {"platform": "sleeper", "leagues": sleeper.discover(Client(self.store), username, season),
                "next_step": "Connect each selected league with its returned user_id; ambiguous ownership requires select_team.",
                "authentication": "Documented public username lookup; no password or session token required."}

    @staticmethod
    def _season(season):
        if isinstance(season, bool) or not isinstance(season, int) or not 1900 <= season <= 2200:
            raise DataError("Season must be an integer year in 1900..2200")

    def select_team(self, alias, team_id):
        """Bind an explicitly chosen, observed team and regenerate its research request."""
        config = deepcopy(self.store.get("config", alias_ok(alias))["data"])
        league = deepcopy(self.snapshot(alias)["data"])
        team_id = str(team_id)
        if team_id not in league["teams"]:
            raise DataError("Select an exact team ID shown by league_context or league_details(teams)")
        config["own_team_id"] = league["own_team_id"] = team_id
        # An explicit user choice must not be overwritten by a later username lookup.
        config["user_id"] = None
        self.store.put("config", alias, config)
        self._publish(alias, league, league.get("source_receipts", []), [])
        return self.context(alias)

    def onboarding_status(self, alias=None):
        """Local readiness diagnostics; never return authentication material."""
        aliases = [alias_ok(alias)] if alias else self.store.keys("config")
        rows = []
        for name in aliases:
            config = self.store.get("config", name)["data"]
            snap = self.store.get("league", name, required=False)
            d = snap["data"] if snap else {}
            selected = d.get("own_team_id") in d.get("teams", {})
            research = league_research.summary(self.store, d, None) if snap else {"status": "not_acquired"}
            rows.append({"alias": name, "platform": config["platform"],
                         "team_selected": selected, "snapshot_available_at": snap["available_at"] if snap else None,
                         "research": research, "forecast_rows": len(self.forecast_rows(name)) if snap else 0,
                         "next_steps": (["select_team with an observed team ID"] if not selected else []) +
                         (["league_research_plan, inspect references, then save_league_research"] if research.get("status") != "saved_agent_research" else [])})
        return {"leagues": rows, "espn_environment_credentials_configured": bool(os.environ.get("ESPN_SWID") and os.environ.get("ESPN_S2")),
                "espn_account_connection": "Public league: numeric ID or ESPN league URL. Private league: configure ESPN_SWID and ESPN_S2 in the local process environment and use auth_mode=environment. Never send credentials in chat. No ESPN account-discovery API is offered.",
                "next_step": "discover_sleeper(username, season), then connect selected leagues" if not rows else "Resolve listed next_steps; refresh dynamic league and player evidence before decisions.",
                "scope": "Local configuration checks only; this does not verify network access, credential validity, source accuracy, or strategy quality."}

    def connect(self, alias, platform, league_id, season, own_team_id=None, user_id=None, auth_mode="none"):
        alias_ok(alias)
        self._season(season)
        if platform == "espn" and str(league_id).startswith("https://"):
            parsed = urlparse(str(league_id))
            if parsed.hostname not in ("fantasy.espn.com", "www.espn.com", "espn.com"):
                raise DataError("Use an ESPN league URL or numeric league ID")
            ids = parse_qs(parsed.query).get("leagueId", [])
            if len(ids) != 1:
                raise DataError("ESPN league URL must contain one leagueId")
            league_id = ids[0]
        sleeper.numeric(league_id, "league ID")
        if platform not in ("sleeper", "espn"):
            raise DataError("Live adapters currently support Sleeper and ESPN; other providers require a verified adapter")
        if platform == "espn" and auth_mode not in ("none", "environment", "firefox"):
            raise DataError("Unsupported ESPN authentication mode")
        config = {"alias": alias, "platform": platform, "league_id": str(league_id), "season": int(season),
                  "own_team_id": str(own_team_id) if own_team_id is not None else None,
                  "user_id": user_id, "auth_mode": auth_mode}
        previous = self.store.get("config", alias, required=False)
        if previous and any(previous["data"].get(k) != config[k] for k in ("platform", "league_id", "season")):
            raise DataError("This alias already belongs to a different provider, league or season. Use a new alias to preserve historical evidence and avoid colliding player identities.")
        # Validate a complete fetch before publishing the new configuration.
        result = self._fetch(config, fresh=True)
        self.store.put("config", alias, config)
        self._publish(alias, *result)
        return self.context(alias)

    def leagues(self):
        rows = []
        for alias in self.store.keys("config"):
            c = self.store.get("config", alias)["data"]
            snap = self.store.get("league", alias, required=False)
            rows.append({"alias": alias, "platform": c["platform"], "league_id": c["league_id"], "season": c["season"],
                         "name": snap["data"].get("name") if snap else None,
                         "last_successful_snapshot": snap["available_at"] if snap else None})
        return rows

    def _fetch(self, config, fresh=False):
        client = Client(self.store, force=fresh)
        if config["platform"] == "sleeper":
            league, receipts, raw, catalog = sleeper.fetch(client, config)
            self.store.put("catalog", "sleeper", catalog, {"source": "documented Sleeper daily catalog"})
            return league, receipts, []
        from . import espn
        url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{config['season']}/segments/0/"
               f"leagues/{config['league_id']}?view=mTeam&view=mRoster&view=mMatchup&view=mSettings&view=mStandings")
        raw, receipt = client.get(url, ttl=60, headers=espn_headers(config["auth_mode"]), namespace="espn:" + config["league_id"])
        league = espn.normalize(raw, config["league_id"], config["season"], config.get("own_team_id"))
        self.store.put("espn_raw", config["league_id"] + ":" + str(config["season"]), raw, receipt)
        rows = espn.projections(raw, league, receipt["checked_at"])
        for row in rows:
            row["source_url"] = url
            row["provider_updated_at"] = None
        return league, [receipt], rows

    def _publish(self, alias, league, receipts, forecasts):
        if not league.get("teams") or not league.get("rules", {}).get("starters"):
            raise DataError("Incomplete normalized league; previous snapshot retained")
        old = self.store.get("league", alias, required=False)
        if old and len(old["data"]["teams"]) != len(league["teams"]):
            self.store.event("team_count_changed", {"alias": alias, "before": len(old["data"]["teams"]), "after": len(league["teams"])})
        profile = strategy_profile(league)
        key = profile["rules_fingerprint"]
        if not self.store.get("strategy", key, required=False):
            self.store.put("strategy", key, profile, {"compiled_from": alias})
        league["strategy_key"] = key
        league["source_receipts"] = receipts
        plan = league_research.request(league, profile)
        if not self.store.get("research_request", plan["research_key"], required=False):
            self.store.put("research_request", plan["research_key"], plan, {"compiled_from": alias})
        league["research_key"] = plan["research_key"]
        league["research_method_version"] = league_research.METHOD_VERSION
        self.store.put("league", alias, league, {"sources": receipts})
        if forecasts:
            self.store.put("forecasts", alias + ":espn", forecasts, {"sources": receipts})

    def refresh(self, alias):
        config = self.store.get("config", alias_ok(alias))["data"]
        start = time.monotonic()
        try:
            self._publish(alias, *self._fetch(config, fresh=True))
        except Exception as exc:
            self.store.event("refresh_failed", {"alias": alias, "error": str(exc) if isinstance(exc, DataError) else type(exc).__name__})
            raise
        return {"alias": alias, "elapsed_ms": round((time.monotonic()-start)*1000), "snapshot": self.context(alias)}

    def snapshot(self, alias, fresh=False, as_of=None):
        if fresh and as_of is not None:
            raise DataError("Historical as-of queries cannot refresh future evidence")
        if fresh:
            self.refresh(alias)
        return self.store.get("league", alias_ok(alias), as_of)

    def context(self, alias, fresh=False, as_of=None):
        record = self.snapshot(alias, fresh, as_of)
        d = record["data"]
        teams = []
        for tid, t in d["teams"].items():
            counts = Counter(p for pid in t["player_ids"] for p in d["players"][pid].get("positions", []))
            teams.append({"id": tid, "name": t["name"], "players": len(t["player_ids"]), "position_counts": dict(counts), "record": t.get("record", {})})
        mine = d.get("own_team_id")
        return {"alias": alias, "platform": d["platform"], "league_id": d["league_id"], "name": d["name"], "format": d["format"],
                "season": d["season"], "week": d.get("week"), "own_team_id": mine,
                "snapshot_available_at": record["available_at"], "snapshot_age_seconds": round(time.time()-record["available_at"], 1),
                "snapshot_hash": record["sha256"], "strategy_key": d["strategy_key"],
                "roster_slots": [s["label"] for s in d["rules"]["starters"]], "bench_slots": d["rules"].get("bench_slots"),
                "teams": teams, "my_roster": self._team_view(d, mine) if mine else None,
                "completeness": d.get("completeness", {}), "source_receipts": d.get("source_receipts", []),
                "research": league_research.summary(self.store, d, as_of),
                "onboarding_status": "ready_for_research" if mine else "team_selection_required",
                "next_tools": (["select_team", "league_details"] if not mine else ["league_research_plan", "league_strategy", "league_details", "player_evidence", "trade_packet"])}

    @staticmethod
    def _team_view(league, tid):
        team = league["teams"][str(tid)]
        return {**team, "players": [{"id": p, **{k:v for k,v in league["players"][p].items() if k != "raw_stats"}} for p in team["player_ids"]]}

    def details(self, alias, section, team_id=None, as_of=None):
        d = self.snapshot(alias, as_of=as_of)["data"]
        if section == "teams":
            return [self._team_view(d, str(team_id))] if team_id is not None else [self._team_view(d, t) for t in d["teams"]]
        if section not in ("rules", "schedule", "picks", "transactions", "completeness"):
            raise DataError("Unknown league detail section")
        return d.get(section, {"status": "not_acquired"})

    def strategy(self, alias, expanded=False, as_of=None):
        d = self.snapshot(alias, as_of=as_of)["data"]
        p = self.store.get("strategy", d["strategy_key"], as_of)["data"]
        p["league"] = {k:d.get(k) for k in ("platform", "league_id", "name", "season", "format")}
        p["research"] = league_research.summary(self.store, d, as_of)
        if expanded:
            saved = self.store.get("league_research", p["research"]["research_key"], as_of, required=False)
            p["research_report"] = saved["data"] if saved else None
            return p
        return {"research_version": p["research_version"], "rules_fingerprint": p["rules_fingerprint"],
                "objective":p.get("objective"), "competition_structure":p.get("competition_structure"),
                "league": p["league"], "horizon": p["horizon"], "operative_priorities": p["operative_priorities"],
                "actions": p["action_checklist"], "missing_rules": p["missing_rules"],
                "research": p["research"],
                "scoring": {"independent_rescoring_complete": p["scoring_support"]["complete"],
                            "provider_native_totals": "May be compared as an attributed baseline with exact league scoring provenance; not an independently verified rescore"},
                "more": "league_strategy(expanded=true) returns rule evidence, methods and falsification tests"}

    def research_plan(self, alias, as_of=None):
        league = self.snapshot(alias, as_of=as_of)["data"]
        profile = self.store.get("strategy", league["strategy_key"], as_of)["data"]
        historical = self.store.get("research_request", league["research_key"], as_of, required=False) if as_of is not None and league.get("research_key") else None
        plan = historical["data"] if historical else league_research.request(league, profile)
        plan["progress"] = league_research.summary(self.store, league, as_of)
        return plan

    def save_research(self, alias, report):
        # Always bind to current rules. Historical reports are read-only views;
        # imports are available now and can never be backdated.
        plan = self.research_plan(alias)
        validated = league_research.validate_report(report, plan)
        receipt = self.store.put("league_research", plan["research_key"], validated,
                                 {"alias": alias, "author": "calling_agent", "method_version": league_research.METHOD_VERSION})
        return {"status": "saved_agent_research", "research_key": plan["research_key"],
                "receipt": receipt, "assurance": validated["assurance"]}

    def search(self, alias, query, limit=10):
        d = self.snapshot(alias)["data"]
        def text_key(value):
            return re.sub(r"[^a-z0-9]","",value.casefold())
        q = text_key(query)
        result = []
        players = dict(d["players"])
        pool = self.store.get("pool",alias,required=False)
        if pool:
            for row in pool["data"].get("players",[]):
                players.setdefault(row["player_id"],row)
        for pid, p in players.items():
            if q in text_key(p["name"]) or query == pid:
                owner = next((tid for tid,t in d["teams"].items() if pid in t["player_ids"]), None)
                result.append({"id": pid, "name": p["name"], "positions": p["positions"], "owner_team_id": owner})
        return result[:min(30,max(1,limit))]

    def evidence(self, alias, player_ids, question=None, expanded=False, as_of=None, decision_at=None):
        d = self.snapshot(alias, as_of=as_of)["data"]
        cutoff = timestamp(decision_at) if decision_at is not None else timestamp(as_of)
        result = []
        if len(player_ids) > 12:
            raise DataError("Request at most twelve focused players per evidence packet")
        for pid in player_ids:
            pid = str(pid)
            if pid not in d["players"]:
                pool = self.store.get("pool",alias,as_of,required=False)
                p = next((x for x in pool["data"].get("players",[]) if x["player_id"]==pid),None) if pool else None
                if p is None:
                    raise DataError(f"Unknown player ID for this league/preserved pool: {pid}")
            else:
                p = d["players"][pid]
            ref = d["platform"] + ":" + pid
            saved = self.store.get("dossier", ref, as_of, required=False)
            gaps = []
            if not saved:
                gaps.append("No preserved dossier; research the causal links that could change this trade")
            elif cutoff - saved["data"].get("evidence_checked_at", 0) > 86400:
                gaps.append("Review volatile role/availability evidence if it can reverse this decision; preserve older structural evidence")
            if saved:
                gaps.extend(saved["data"].get("open_questions",[]))
            status = p.get("injury_status")
            label = status.get("status") if isinstance(status,dict) else status
            if str(label).upper() in ("OUT","IR","QUESTIONABLE","DOUBTFUL","PUP","SUSPENDED","INACTIVE"):
                gaps.append("Verify dated official availability and post-return role; status is not a recovery distribution")
            if d["format"] == "keeper":
                gaps.append("Price retention only after keeper eligibility, cost, escalation and limits are known")
            if d["format"] == "dynasty":
                gaps.append("Test future service, information timing and feasible replacement; do not inherit an age-only curve")
            result.append({"player_id": pid, "name": p["name"], "current_identity": {k:v for k,v in p.items() if k != "raw_stats"},
                           "dossier_ref": ref if saved else None,
                           "preserved_dossier": (saved["data"] if expanded else {
                               "summary": ("Archived dynasty draft research. Load expanded evidence for reusable observations; its league-specific valuations do not transfer automatically." if saved["data"].get("archive_import") and d["format"] != "dynasty" else saved["data"].get("summary")),
                               "evidence_checked_at": saved["data"].get("evidence_checked_at"),
                               "reversal_conditions": ("Recheck role, availability and the current roster substitution in this league." if saved["data"].get("archive_import") and d["format"] != "dynasty" else saved["data"].get("reversal_conditions")),
                               "claim_count":len(saved["data"].get("claims",[])),
                               "source_count":len(saved["data"].get("sources",[])),
                               "more":"expanded=true retrieves full claims, sources and archived review; old strategic judgments are hypotheses, not this league's strategy",
                           }) if saved else None,
                           "research_status": "refresh_required" if gaps else "usable_subject_to_question",
                           "research_tasks": gaps, "decision_question": question,
                           "instruction": "Calling session researches only decision-sensitive gaps; the server does not generate unsupported football claims."})
        return result

    def save_dossier(self, player_ref, dossier):
        if not re.fullmatch(r"(sleeper|espn):[A-Za-z0-9_-]+", player_ref):
            raise DataError("Use a namespaced player reference")
        for key in ("claims", "sources", "evidence_checked_at", "reversal_conditions"):
            if key not in dossier:
                raise DataError(f"Dossier requires {key}")
        if not dossier["sources"]:
            raise DataError("A dossier needs source evidence; model confidence alone is not a source")
        dossier = deepcopy(dossier)
        dossier["evidence_checked_at"] = timestamp(dossier["evidence_checked_at"])
        if dossier["evidence_checked_at"] > time.time()+60:
            raise DataError("Evidence check time cannot be in the future")
        return self.store.put("dossier", player_ref, dossier, {"authored_by": "calling_session", "independent_verification": False})

    def forecast_rows(self, alias, as_of=None):
        result = []
        for key in self.store.keys("forecasts", as_of):
            if key.startswith(alias + ":"):
                record = self.store.get("forecasts", key, as_of)
                rows = deepcopy(record["data"])
                provenance_sources = record.get("provenance", {}).get("sources", [])
                urls = sorted({
                    source.get("url") for source in provenance_sources
                    if isinstance(source, dict) and isinstance(source.get("url"), str)
                })
                for row in rows:
                    if not row.get("source_url") and len(urls) == 1:
                        row["source_url"] = urls[0]
                    row["local_batch_available_at"] = record["available_at"]
                result.extend(rows)
        return result

    def refresh_forecasts(self, alias, weeks=None, force=False):
        """Fetch observed ESPN weekly rows without replacing the current roster snapshot."""
        from . import espn
        c = self.store.get("config", alias_ok(alias))["data"]
        d = self.snapshot(alias)["data"]
        if c["platform"] != "espn":
            return {"status": "provider_import_required", "reason": "Sleeper public API does not document forecasts. Import a licensed source with explicit scoring and period."}
        weeks = weeks or [d["week"]]
        if len(weeks) > 18 or any(isinstance(w,bool) or not isinstance(w,int) or not 1 <= w <= 18 for w in weeks):
            raise DataError("Request distinct weeks in 1..18")
        client = Client(self.store, force=force)
        headers = espn_headers(c["auth_mode"])
        receipts = []
        for week in sorted(set(weeks)):
            url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{c['season']}/segments/0/"
                   f"leagues/{c['league_id']}?view=mTeam&view=mRoster&view=mMatchup&view=mSettings&view=mStandings&scoringPeriodId={week}")
            raw, receipt = client.get(url, ttl=900, headers=headers, namespace="espn:"+c["league_id"])
            normalized = espn.normalize(raw, c["league_id"], c["season"], c.get("own_team_id"))
            if digest(normalized["rules"]["scoring"]) != digest(d["rules"]["scoring"]):
                raise DataError("Scoring changed during forecast fetch; refresh league before using these forecasts")
            self.store.put("espn_future_raw", f"{c['league_id']}:{c['season']}:{week}", raw, receipt)
            rows = [r for r in espn.projections(raw, normalized, receipt["checked_at"]) if r["period"]["week"] == week]
            for row in rows:
                row["source_url"] = url
                row["provider_updated_at"] = None
            if not rows:
                raise DataError(f"Provider returned no projections for requested week {week}; old batch retained")
            self.store.put("forecasts", f"{alias}:espn:week{week}", rows, receipt)
            receipts.append({"week": week, "rows":len(rows), **receipt})
        return {"status":"acquired", "source":"espn", "receipts":receipts, "interpretation":"Provider baseline; no independent accuracy or availability-conditioning validation"}

    def import_forecasts(self, alias, source, rows):
        d = self.snapshot(alias)["data"]
        pool = self.store.get("pool", alias, required=False)
        pool_bound = pool and pool["data"].get("league_binding") == {k: d[k] for k in ("platform", "league_id", "season")}
        known_ids = set(d["players"]) | ({str(p["player_id"]) for p in pool["data"]["players"]} if pool_bound else set())
        if not rows or len(rows) > 25000:
            raise DataError("Supply a nonempty, bounded forecast batch")
        now, seen, validated = time.time(), set(), []
        for r in rows:
            r = deepcopy(r)
            if str(r.get("player_id")) not in known_ids or r.get("season") != d["season"]:
                raise DataError("Forecast identity or season is not matched to this league")
            if r.get("source") != source or not r.get("period") or not r.get("source_url"):
                raise DataError("Each forecast needs source, source_url and explicit period")
            if r.get("period", {}).get("kind") not in ("week", "season", "rest_of_season"):
                raise DataError("Unsupported forecast period")
            for val in ([r["points"]] if "points" in r else []) + list(r.get("components", {}).values()):
                if isinstance(val,bool) or not isinstance(val,(int,float)) or not math.isfinite(val):
                    raise DataError("Forecast values must be finite numbers, never missing-as-zero")
            key = (str(r["player_id"]), digest(r["period"]))
            if key in seen:
                raise DataError("Duplicate player/period within forecast batch")
            seen.add(key)
            r["original_available_at"] = r.get("available_at")
            r["available_at"] = now
            r["player_id"] = str(r["player_id"])
            validated.append(r)
        return self.store.put("forecasts", alias + ":" + source, validated, {"scope": "caller-supplied licensed/exported forecast; not independently accuracy-certified"})

    def trade_packet(self, alias, proposal, fresh=True, unavailable=None, as_of=None, expanded=False):
        from .trades import compare_trade
        if not isinstance(proposal, dict):
            raise DataError("Trade proposal must be an object")
        proposal = deepcopy(proposal)
        explicit_decision = "decision_at" in proposal
        if explicit_decision and proposal["decision_at"] is None:
            raise DataError("decision_at must be a number or timezone-aware ISO date")
        requested_decision = timestamp(proposal["decision_at"]) if explicit_decision else None
        requested_as_of = timestamp(as_of) if as_of is not None else None
        now = time.time()
        for label, value in (("decision_at", requested_decision), ("as_of", requested_as_of)):
            if value is not None and value > now + 60:
                raise DataError(f"{label} cannot be in the future")

        if explicit_decision or requested_as_of is not None:
            # A pinned cutoff must use only versions already knowable then. A
            # refresh would create evidence after that point, so it is skipped.
            read_cutoff = min(value for value in (requested_decision, requested_as_of) if value is not None)
            decision_at = requested_decision if requested_decision is not None else requested_as_of
            snap = self.snapshot(alias, fresh=False, as_of=read_cutoff)
            refresh_performed = False
        else:
            snap = self.snapshot(alias, fresh=fresh)
            decision_at = time.time()
            read_cutoff = decision_at
            refresh_performed = bool(fresh)
        if snap["available_at"] > read_cutoff:
            raise DataError("Snapshot was not knowable at the packet evidence cutoff")

        d = snap["data"]
        proposal["decision_at"] = decision_at
        result = compare_trade(d, proposal, self.forecast_rows(alias, read_cutoff), unavailable)
        from .trade_decision import frame
        pids = sorted({x["player_id"] for x in result.get("proposal", {}).get("transfers", [])})
        involved = [t for t in result.get("affected_team_ids", []) if t in d["teams"]]
        packet = {"league": {"alias": alias, "name": d["name"], "format": d["format"], "season": d["season"], "week": d.get("week")},
                  "decision_at": decision_at, "evidence_as_of": read_cutoff,
                  "refresh_performed": refresh_performed,
                  "snapshot_hash": snap["sha256"], "snapshot_available_at": snap["available_at"],
                  "snapshot_age_seconds_at_decision": max(0.0, decision_at - snap["available_at"]),
                  "snapshot_sources": d.get("source_receipts", []),
                  "strategy": self.strategy(alias, as_of=read_cutoff),
                  "proposal": proposal, "mechanics": result,
                  "decision_support": frame(d, result),
                  "teams": [self._team_view(d, t) for t in involved],
                  "focused_evidence": self.evidence(alias, [p for p in pids[:12] if p in d["players"]], as_of=read_cutoff, decision_at=decision_at),
                  "decision_questions": ["What useful lineup/coverage function changes for EACH team?", "Which one uncertain causal link could reverse the decision?",
                                         "What is given up versus holding and each named, currently available waiver or trade alternative?",
                                         "How do waiver priority, a deliberate lineup choice, and the cost of a lost win change the option set under this league's playoff rules?",
                                         "Why could the counterparty accept, and why could it decline?"],
                  "required_counterfactuals": {
                      "hold": {
                          "operation": "construct_named_legal_hold_lineup_and_coverage",
                          "requires": ["current deployability", "all starter slots", "bench and reserve functions"],
                      },
                      "available_alternatives": {
                          "operation": "enumerate_named_verified_pool_or_owner_alternatives",
                          "requires": ["current availability receipt", "acquisition cost", "resulting legal roster and lineup"],
                          "prohibition": "No abstract replacement player.",
                      },
                      "waiver_state": {
                          "operation": "compare_spend_or_priority_use_against_preservation",
                          "requires": ["current priority or budget", "pending claims", "reset rule", "named available candidates"],
                          "observed_rule": d.get("rules", {}).get("waivers"),
                      },
                      "standings_path": {
                          "operation": "evaluate_win_loss_effect_on_observed_qualifying_paths",
                          "requires": ["current record and schedule", "declared lineup intent", "waiver-order consequence"],
                          "observed_playoff_field": {
                              "qualifying_teams": d.get("rules", {}).get("playoffs", {}).get("teams"),
                              "league_teams": len(d.get("teams", {})),
                              "start_week": d.get("rules", {}).get("playoffs", {}).get("start_week"),
                              "round_weeks": d.get("rules", {}).get("playoffs", {}).get("round_weeks"),
                          },
                          "output_constraint": "Scenario effects only; no universal or unsupported title percentage.",
                      },
                  },
                  "verdict": None, "championship_probability_delta": None,
                  "scope": "Evidence and accounting only. The calling session owns the recommendation and confidence. No trade is sent."}
        receipt = self.store.put("trade_packet", alias, packet, {"snapshot_version": snap["version_id"]})
        if expanded:
            return {**packet, "packet_receipt": receipt}
        compact = {k:v for k,v in packet.items() if k not in ("mechanics", "teams")}
        compact["mechanics"] = self._compact_comparison(result, d)
        compact["team_rosters"] = [{"id":t, "name":d["teams"][t]["name"], "players":[{"id":p,"name":d["players"][p]["name"],"positions":d["players"][p]["positions"],"reserve":p in d["teams"][t].get("reserve", [])} for p in d["teams"][t]["player_ids"]]} for t in involved]
        return {**compact, "packet_receipt": receipt, "expanded_packet": "trade_packet(expanded=true) or saved_packet(alias)"}

    @staticmethod
    def _compact_comparison(result, league):
        def named(ids):
            return [{"id":p,"name":league["players"].get(p,{}).get("name",p)} for p in ids]
        cohorts = []
        for c in result.get("forecast_comparisons", []):
            teams = {}
            for t, v in c["teams"].items():
                before, after = set(v["before"]["selected_player_ids"]),set(v["after"]["selected_player_ids"])
                before_slots={r["player_id"]:r["slot_label"] for r in v["before"]["selected"] if r["player_id"]}
                after_slots={r["player_id"]:r["slot_label"] for r in v["after"]["selected"] if r["player_id"]}
                teams[t] = {
                    "before_total": v["before"]["total_points"],
                    "after_total": v["after"]["total_points"],
                    "delta_provider_baseline_points": v["delta_points"],
                    "before_starters": named(v["before"]["selected_player_ids"]),
                    "after_starters": named(v["after"]["selected_player_ids"]),
                    "new_starters":named(sorted(after-before)),
                    "displaced_starters":named(sorted(before-after)),
                    "still_scores_but_moves_slot":[{"id":p,"name":league["players"][p]["name"],"from":before_slots[p],"to":after_slots[p]} for p in sorted(before & after) if before_slots[p] != after_slots[p]],
                    "marginal_equation":"Compare only new_starters minus displaced_starters; common players still score even when moving between RB/WR/FLEX. Do not count their points a second time.",
                }
            cohorts.append({
                "source": c["source"], "period": c["period"], "status": c["status"],
                "conditioning": c["conditioning"], "interpretation": c.get("interpretation"),
                "use": "Attributed provider baseline for a legal-lineup counterfactual; not a recommendation authority or fair-value grade.",
                "forecast_available_at_oldest": c.get("forecast_available_at_oldest"),
                "forecast_available_at_newest": c.get("forecast_available_at_newest"),
                "forecast_age_seconds_at_decision": c.get("forecast_age_seconds_at_decision"),
                "source_urls": c.get("source_urls", []),
                "provider_updated_at": c.get("provider_updated_at", []),
                "scoring_basis_counts": dict(Counter(c.get("scoring_basis_by_player", {}).values())),
                "missing_players": named(c["missing_required_player_ids"]), "teams": teams,
                "limitation": c.get("lineup_timing_limitation") or c.get("best_ball_limitation") or c.get("capacity_limitation"),
            })
        roster_changes = {}
        for team_id, team in result.get("teams", {}).items():
            roster_changes[team_id] = {
                "incoming": named(team["incoming_player_ids"]),
                "outgoing": named(team["outgoing_player_ids"]),
                "drops": named(team["dropped_player_ids"]),
                "cardinality": {"before": team.get("before_capacity"), "after": team["capacity"]},
                "position_capacity": {"before": team.get("before_position_capacity"), "after": team.get("position_capacity")},
            }
        return {"valid":result["valid"],
                "roster_feasibility":result.get("roster_feasibility"),
                "transaction_executable":None,
                "mechanics_scope":result.get("mechanics_scope"),
                "execution_scope":"Ownership, roster cardinality, and translated position limits only; locks, review, deadline, veto and counterparty consent still require confirmation",
                "errors":result.get("errors",[]),"warnings":result.get("warnings",[]),
                "roster_changes":roster_changes,
                "comparisons":cohorts, "evidence_needed":result.get("evidence_needed",[]),
                "rejected_forecast_counts":dict(Counter(x["code"] for x in result.get("forecast_rejections",[])))}
