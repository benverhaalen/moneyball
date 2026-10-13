"""Persistent decision preparation. Evidence and mechanics, never agent verdicts.

Contexts bind locally knowable versions at one cutoff. Rechecking creates a new
revision; it cannot silently change a comparison made against an older revision.
"""
from copy import deepcopy
import time
import uuid
from .store import DataError, digest, timestamp

INTENTS = {"trade", "trade_discovery", "waiver", "lineup", "draft", "rebuild", "research", "mixed"}


def _load(service, decision_id):
    return service.store.get("decision", decision_id)["data"]


def _manifest(service, alias, cutoff, league, focus):
    # All reads share a fixed knowability cutoff. Appends after that cutoff are
    # excluded even if ingestion runs concurrently. These are exact version IDs,
    # not provider publication claims or aliases pointing at changing records.
    refs = []
    for kind in ("league", "pool", "pool_index", "research_request", "league_research", "strategy", "forecasts", "dossier", "news_leads", "player_identity_binding", "identity_source"):
        if kind in ("league", "pool", "pool_index"):
            keys = [alias]
        elif kind == "strategy":
            keys = [league["strategy_key"]]
        elif kind in ("research_request", "league_research"):
            keys = [league["research_key"]]
        elif kind in ("dossier", "news_leads", "player_identity_binding"):
            keys = [league["platform"] + ":" + p for p in focus]
        elif kind == "identity_source":
            keys = ["warehouse"] if focus else []
        else:
            keys = service.store.keys(kind, cutoff)
        for key in keys:
            if kind == "forecasts" and not key.startswith(alias + ":"):
                continue
            record = service.store.get(kind, key, cutoff, required=False)
            if record:
                refs.append({"kind": kind, "key": key, "version_id": record["version_id"],
                             "sha256": record["sha256"], "available_at": record["available_at"]})
    from .evidence_bridge import index_manifest_for_decision
    refs.extend(index_manifest_for_decision(service.store, league, focus, asof=cutoff))
    return refs


def prepare(service, specification, *, decision_id=None, revision=1):
    if not isinstance(specification, dict):
        raise DataError("Decision specification must be an object")
    spec = deepcopy(specification)
    question = spec.get("question")
    if not isinstance(question, str) or not question.strip():
        raise DataError("Decision requires the user's question")
    alias = spec.get("alias")
    if not alias:
        refs = spec.get("league_refs", [])
        if len(refs) == 1 and isinstance(refs[0], dict):
            alias = refs[0].get("alias")
    if not isinstance(alias, str) or not alias:
        raise DataError("Resolve one explicit league alias; never infer the most recent league")
    intent = spec.get("intent", "mixed")
    if intent not in INTENTS:
        raise DataError("Unknown decision intent; use mixed for an unanticipated question")
    historical = spec.get("as_of") is not None
    deadline_at = timestamp(spec["deadline"]) if spec.get("deadline") is not None else None
    # One cumulative network budget, not a fresh timeout for every endpoint.
    # The returned evidence can remain useful after timeout, but never becomes
    # current merely because preparation completed.
    network_deadline = time.monotonic() + min(30.0, max(0.0, deadline_at - time.time())) if deadline_at is not None else None
    cutoff = timestamp(spec["as_of"]) if historical else None
    if historical and cutoff > time.time():
        raise DataError("Historical cutoff cannot be in the future")
    gaps, refresh = [], {"requested": not historical and spec.get("refresh", True), "status": "not_requested"}
    if refresh["requested"]:
        try:
            if network_deadline is not None:
                service.refresh(alias, deadline=network_deadline)
            else:
                service.refresh(alias)
            refresh["status"] = "completed"
        except (DataError, OSError, TimeoutError) as exc:
            refresh["status"] = "failed"
            refresh["error_type"] = type(exc).__name__
            gaps.append({"code": "league_refresh_failed", "effect": "Last successful snapshot retains its original date; recheck before action"})
    maintenance = None
    if spec.get("maintenance_job_ids") is not None:
        if historical:
            raise DataError("Historical preparation cannot run current maintenance")
        maintenance = service.run_maintenance(spec["maintenance_job_ids"], deadline=spec.get("deadline"))
        if any(job.get("outcome") != "success" for job in maintenance.get("jobs", [])) or maintenance.get("deadline_exhausted"):
            gaps.append({"code": "maintenance_partial", "effect": "Inspect individual acquisition receipts; old successful dates remain explicit"})
    news = None
    discover_news = spec.get("discover_news", True)
    if not isinstance(discover_news, bool):
        raise DataError("discover_news must be boolean")
    if discover_news and spec.get("player_ids"):
        # Leads never clear reporting gaps. Freeze only after discovery finishes;
        # historical/cached questions cannot fetch today's reports into the past.
        news = service.news_leads(alias, spec["player_ids"],
            as_of=cutoff if historical else None if refresh["requested"] else time.time(),
            deadline=min(network_deadline, time.monotonic()+10) if network_deadline is not None else time.monotonic()+10)
    cutoff = time.time() if cutoff is None else cutoff
    if deadline_at is not None and time.time() >= deadline_at:
        gaps.append({"code": "decision_deadline_elapsed", "effect": "Cached conditional evidence only; no fresh action certification"})
    _prepare_evidence_index(service, alias, spec.get("player_ids", []), cutoff)
    with service.store.read_view(cutoff):
        context = _assemble(service, spec, alias, intent, question, cutoff, gaps, refresh,
                            decision_id=decision_id, revision=revision)
    if maintenance is not None:
        context["maintenance"] = maintenance
    if news is not None:
        context["news_discovery"] = news
    service.store.put("decision", context["decision_id"], context, {"role": "decision_context", "revision": revision})
    return context


def _prepare_evidence_index(service, alias, focus, cutoff):
    """Materialize locally known dossier claims before pinning read-only state.

    This is a derived index migration, never fresh reporting or backdated capture.
    Only the bounded requested players are examined, not the whole catalog.
    """
    if not isinstance(focus, list) or len(focus) > 12 or any(not isinstance(x, str) for x in focus):
        raise DataError("Focus requires at most twelve exact provider player IDs")
    from .evidence_bridge import migrate_dossiers
    league = service.snapshot(alias, as_of=cutoff)["data"]
    migrate_dossiers(service.store, [league["platform"] + ":" + pid for pid in focus], as_of=cutoff)


def _assemble(service, spec, alias, intent, question, cutoff, gaps, refresh, *, decision_id, revision):
    snapshot = service.snapshot(alias, as_of=cutoff)
    league = snapshot["data"]
    for ref in spec.get("league_refs", []):
        if not isinstance(ref, dict) or ref.get("alias", alias) != alias:
            raise DataError("Decision league reference conflicts with the explicit alias")
        for key in ("platform", "league_id", "season", "own_team_id"):
            if key in ref and str(ref[key]) != str(league.get(key)):
                raise DataError(f"Decision league reference {key} conflicts with observed identity")
    own = league.get("own_team_id")
    if own is None or str(own) not in league["teams"]:
        gaps.append({"code": "own_team_required", "effect": "Select an observed team before personalized recommendations"})
    focus = spec.get("player_ids", [])
    if not isinstance(focus, list) or len(focus) > 12 or any(not isinstance(x, str) for x in focus):
        raise DataError("Focus requires at most twelve exact provider player IDs")
    strategy = service.strategy(alias, as_of=cutoff)
    if strategy.get("research", {}).get("status") != "saved_agent_research":
        gaps.append({"code": "strategy_research_not_confirmed", "effect": "Reuse observed mechanics; inspect research status, do not claim completed discovery"})
    if league["format"] in ("dynasty", "keeper") and not spec.get("horizon"):
        gaps.append({"code": "retention_horizon_unspecified", "effect": "Show current/future frontier rather than selecting an implicit discount rate"})
    forecasts = service.forecast_rows(alias, cutoff)
    if not forecasts:
        gaps.append({"code": "forecasts_missing", "effect": "Numeric contribution comparison requires compatible dated forecasts"})
    pool = service.store.get("pool_index", alias, cutoff, required=False) or service.store.get("pool", alias, cutoff, required=False)
    if intent in ("waiver", "trade_discovery", "rebuild") and pool is None:
        gaps.append({"code": "acquisition_universe_missing", "effect": "No verified outside option; obtain pool before pruning alternatives"})
    evidence = service.evidence(alias, focus, question=question, as_of=cutoff) if focus else []
    from .evidence_bridge import evidence_for_decision
    from .source_readiness import prepare_sources
    indexed = evidence_for_decision(service.store, league, focus, asof=cutoff)
    sources = prepare_sources(service, alias, league, spec, forecasts, as_of=cutoff)
    gaps.extend(sources["gaps"])
    if sources["reporting"]["status"] != "targeted_reports_present":
        gaps.append({"code": "news_coverage_not_certified", "effect": "No complete current targeted-report coverage; absence is not no news"})
    manifest = _manifest(service, alias, cutoff, league, focus)
    identity = {k: league.get(k) for k in ("platform", "league_id", "season", "own_team_id", "format")}
    context = {"decision_id": decision_id or uuid.uuid4().hex, "revision": revision,
               "specification": spec, "alias": alias, "question": question, "intent": intent,
               "objective": spec.get("objective", "Improve attainable championship outcomes under the user's declared horizon"),
               "horizon": spec.get("horizon"), "deadline": spec.get("deadline"), "evidence_as_of": cutoff,
               "league_binding": identity, "rules_hash": digest(league["rules"]),
               "rules": league["rules"], "strategy": strategy,
               "snapshot": {k: snapshot[k] for k in ("version_id", "sha256", "available_at")},
               "refresh": refresh, "manifest": manifest, "manifest_hash": digest(manifest),
               "all_team_index": [{"team_id": str(tid), "name": t["name"], "player_ids": t["player_ids"],
                                   "reserve": t.get("reserve", []), "taxi": t.get("taxi", []),
                                   "record": t.get("record")} for tid, t in sorted(league["teams"].items())],
               "candidate_coverage": {"focused_player_ids": focus, "owned_player_count": len(league["players"]),
                                      "pool_version_id": pool["version_id"] if pool else None,
                                      "searched_universe": "Focused IDs plus all observed roster membership; no automatic candidate ranking",
                                      "exhaustive": False},
               "focused_evidence": evidence, "indexed_evidence": indexed, "source_readiness": sources,
               "forecast_row_count": len(forecasts),
               "readiness": "partial" if gaps else "prepared", "gaps": gaps,
               "expansion": {"evidence": "decision_evidence", "comparison": "compare_actions", "candidates": "discover_candidates", "roster_plan": "roster_plan", "recheck": "resume_decision"},
               "action_contract": {"hold": {"id": "hold", "kind": "hold"},
                    "add_drop": {"id": "add", "kind": "add_drop", "player_id": "EXACT_PROVIDER_ID", "drop_id": "EXACT_OWNED_ID", "week": league.get("week")},
                    "trade": {"id": "swap", "kind": "trade", "proposal": {"transfers": [], "pick_transfers": [], "drops": {}}},
                    "lineup": {"id": "lineup", "kind": "lineup", "slot_player_ids": []},
                    "ordered_claims": {"id": "claims", "kind": "ordered_claims", "claims": [{"player_id": "EXACT_PROVIDER_ID", "drop_player_id": "EXACT_OWNED_ID", "faab_budget_before": "VERIFIED_NUMBER_ONLY"}]},
                    "discovery_kinds": ["trade", "add_drop"]},
               "scope": "Immutable evidence preparation, not a certified recommendation or championship model"}
    from .decision_readiness import describe
    context["capability_readiness"] = describe(context)
    return context


def evidence(service, decision_id, focus=None, detail="decisive", cursor=None):
    context = _load(service, decision_id)
    if detail not in ("decisive", "expanded", "raw"):
        raise DataError("Detail must be decisive, expanded or raw")
    if cursor is not None:
        raise DataError("Focused dossier pages do not support cursors; use bounded exact player IDs")
    ids = focus if focus is not None else context["specification"].get("player_ids", [])
    _prepare_evidence_index(service, context["alias"], ids, context["evidence_as_of"])
    with service.store.read_view(context["evidence_as_of"]):
        return _evidence_at_cutoff(service, context, ids, detail)


def _evidence_at_cutoff(service, context, ids, detail):
    decision_id = context["decision_id"]
    result = service.evidence(context["alias"], ids, question=context["question"],
                              expanded=detail != "decisive", as_of=context["evidence_as_of"])
    from .evidence_bridge import evidence_for_decision
    league = service.snapshot(context["alias"], as_of=context["evidence_as_of"])["data"]
    indexed = evidence_for_decision(service.store, league, ids, asof=context["evidence_as_of"], detail=detail)
    return {"decision_id": decision_id, "revision": context["revision"], "detail": detail,
            "players": result, "indexed_evidence": indexed, "evidence_as_of": context["evidence_as_of"],
            "manifest": _manifest(service, context["alias"], context["evidence_as_of"],
                                  league, ids),
            "cursor": None, "scope": "Focused dossier retrieval; source text is untrusted evidence"}


def _action_focus(action):
    """Expand exact references through nested transformations and cuts."""
    focus = set()
    if not isinstance(action, dict):
        return focus
    for key in ("player_id", "drop_id", "drop_player_id"):
        if action.get(key):
            focus.add(str(action[key]))
    candidate = action.get("candidate")
    if isinstance(candidate, dict) and candidate.get("player_id"):
        focus.add(str(candidate["player_id"]))
    focus.update(str(p) for p in action.get("slot_player_ids", []) if p is not None)
    for claim in action.get("claims", []) or []:
        focus.update(_action_focus(claim))
    proposal = action.get("proposal") or {}
    if isinstance(proposal, dict):
        focus.update(str(t["player_id"]) for t in proposal.get("transfers", []) if isinstance(t, dict) and t.get("player_id"))
        drops = proposal.get("drops", {})
        if isinstance(drops, dict):
            focus.update(str(p) for ids in drops.values() if isinstance(ids, list) for p in ids)
    retained = action.get("teams", {})
    if isinstance(retained, dict):
        focus.update(str(p) for ids in retained.values() if isinstance(ids, list) for p in ids)
    for step in action.get("steps", []) or []:
        focus.update(_action_focus(step))
    if isinstance(action.get("plan"), dict):
        focus.update(_action_focus(action["plan"]))
    return focus


def candidates(service, decision_id, *, limit=20, cursor=None, kinds=None,
               focus_player_ids=None, package_shapes=None, detail="compact"):
    from .candidates import discover_candidates
    context = _load(service, decision_id)
    alias, cutoff = context["alias"], context["evidence_as_of"]
    with service.store.read_view(cutoff):
        league = service.snapshot(alias, as_of=cutoff)["data"]
        pool = service.store.get("pool_index", alias, cutoff, required=False) or service.store.get("pool", alias, cutoff, required=False)
        mapped_kinds = [{"trade":"Trade", "add_drop":"AddDrop"}.get(k, k) for k in kinds] if kinds is not None else None
        page = discover_candidates(league, pool["data"] if pool else None, limit=limit, cursor=cursor,
            kinds=mapped_kinds, focus_player_ids=focus_player_ids, package_shapes=package_shapes,
            forecasts=service.forecast_rows(alias, cutoff), decision_at=cutoff)
        focus = set(context["specification"].get("player_ids", []))
        for row in page["candidates"]:
            focus.update(_action_focus(row["action"]))
        manifest = _manifest(service, alias, cutoff, league, sorted(focus))
    packet = {**page, "decision_id": decision_id, "revision": context["revision"],
            "evidence_as_of": cutoff, "pool_version_id": pool["version_id"] if pool else None,
            "focused_player_ids": sorted(focus), "consumed_manifest": manifest,
            "consumed_manifest_hash": digest(manifest)}
    if detail not in ("compact", "full"):
        raise DataError("Candidate detail must be compact or full")
    if detail == "full":
        return packet
    page_id = digest(packet)
    if service.store.get("candidate_page", page_id, required=False) is None:
        service.store.put("candidate_page", page_id, packet)
    def mechanics(row):
        result = {k: deepcopy(row[k]) for k in ("valid", "executable", "roster_branch_feasible", "errors", "warnings", "evidence_needed", "roster_feasibility", "roster_changes", "forecast_rejections") if k in row}
        result["forecast_summaries"] = [{k: deepcopy(f[k]) for k in ("status", "source", "period", "delta_points", "missing_player_ids", "gaps") if k in f}
                                        for f in row.get("forecast_comparisons", [])]
        result["interpretation"] = "Screening mechanics only; expand retained full comparison before deciding. No title odds or value ranking."
        return result
    compact = {k: deepcopy(v) for k, v in packet.items() if k not in ("candidates", "hold", "all_team_index")}
    compact["hold"] = {"kind": "hold", "comparison": mechanics(page["hold"]["comparison"])}
    compact["candidates"] = [{**{k: deepcopy(v) for k, v in row.items() if k not in ("comparison", "action")},
        "action": _normalize_action(row["action"]), "comparison": mechanics(row["comparison"])} for row in page["candidates"]]
    compact["all_team_index"] = {tid: {k: deepcopy(row[k]) for k in ("capacity", "structural_slot_coverage", "position_membership", "pick_ids")} for tid, row in page["all_team_index"].items()}
    compact["candidate_page_id"] = page_id
    compact["expansion"] = {"tool": "read_candidate_page", "candidate_page_id": page_id,
        "note": "Full original calculations, source qualifications and decision support retained without truncation; compare finalists with explicit hold."}
    return compact


def _normalize_action(raw):
    if not isinstance(raw, dict):
        raise DataError("Each action must be an object")
    action = deepcopy(raw)
    action["kind"] = {"Hold": "hold", "Trade": "trade", "AddDrop": "add_drop"}.get(action.get("kind"), action.get("kind"))
    if action["kind"] == "add_drop":
        for canonical, alternate in (("player_id", "add_player_id"), ("drop_id", "drop_player_id")):
            if alternate in action:
                if canonical in action and action[canonical] != action[alternate]:
                    raise DataError("Conflicting acquisition player/drop identifiers")
                action[canonical] = action.pop(alternate)
        if not isinstance(action.get("player_id"), str) or not action["player_id"]:
            raise DataError("add_drop requires player_id and an explicit drop_id where capacity requires a cut")
    return action


def _roster_plan(service, context, plan):
    from .candidates import compare_roster_plan
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise DataError("Roster plan requires explicit steps")
    alias, cutoff = context["alias"], context["evidence_as_of"]
    league = service.snapshot(alias, as_of=cutoff)["data"]
    normalized = deepcopy(plan)
    pool = service.store.get("pool_index", alias, cutoff, required=False) or service.store.get("pool", alias, cutoff, required=False)
    kind_map = {"hold":"Hold", "trade":"Trade", "add_drop":"AddDrop", "placement":"Placement",
                "retain":"Retain", "release_right":"ReleaseRight", "exercise_right":"ExerciseRight"}
    for step in normalized["steps"]:
        if not isinstance(step, dict):
            raise DataError("Roster plan steps must be objects")
        step["kind"] = kind_map.get(step.get("kind"), step.get("kind"))
        if step["kind"] == "Trade":
            proposal = step.get("proposal")
            if not isinstance(proposal, dict):
                raise DataError("Roster-plan trade requires proposal")
            proposal["decision_at"] = cutoff
        elif step["kind"] == "AddDrop":
            pid = step.get("player_id") or (step.get("candidate") or {}).get("player_id")
            if not pool:
                raise DataError("Roster-plan acquisition requires preserved current pool membership")
            expected = {k: league.get(k) for k in ("platform", "league_id", "season")}
            if pool["data"].get("league_binding") not in (None, expected):
                raise DataError("Roster-plan pool belongs to a different league/season")
            row = next((r for r in pool["data"].get("players", []) if r.get("player_id") == pid), None)
            if row is None:
                raise DataError("Roster-plan target missing from preserved complete pool index")
            step["candidate"] = {**deepcopy(row), **{k:pool["data"][k] for k in ("observed_at", "expires_at", "horizon", "source") if k in pool["data"]}}
            if "observed_at" not in step["candidate"]:
                step["candidate"]["observed_at"] = pool["data"].get("catalog_available_at", pool["available_at"])
            if "horizon" not in step["candidate"]:
                step["candidate"]["horizon"] = {"kind":"week", "season":league["season"], "week":league.get("week")}
            step["drop_player_id"] = step.get("drop_player_id", step.get("drop_id"))
            step["decision_at"] = cutoff
    return compare_roster_plan(league, normalized, service.forecast_rows(alias, cutoff))


def roster_plan(service, decision_id, plan):
    context = _load(service, decision_id)
    with service.store.read_view(context["evidence_as_of"]):
        result = _roster_plan(service, context, plan)
        focus = sorted(set(context["specification"].get("player_ids", [])) | _action_focus(plan))
        league = service.snapshot(context["alias"], as_of=context["evidence_as_of"])["data"]
        manifest = _manifest(service, context["alias"], context["evidence_as_of"], league, focus)
    return {**result, "decision_id": decision_id, "revision": context["revision"],
            "evidence_as_of": context["evidence_as_of"], "focused_player_ids": focus,
            "consumed_manifest": manifest, "consumed_manifest_hash": digest(manifest)}


def compare(service, decision_id, actions, worlds=None):
    context = _load(service, decision_id)
    with service.store.read_view(context["evidence_as_of"]):
        comparison = _compare(service, decision_id, actions, worlds, context)
    service.store.put("decision_comparison", comparison["comparison_id"], comparison)
    return comparison


def compact_comparison(comparison):
    """Remove repeated full snapshots, never forecast qualifications or mechanics.

    Exact originals already live in decision_comparison. Forecast before/after
    selected lineups remain inline; only entire league snapshots are replaced.
    """
    def visit(value, path):
        if isinstance(value, list):
            return [visit(v, path + [i]) for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return deepcopy(value)
        if all(k in value for k in ("platform", "rules", "teams", "players")):
            return {"retained_full_state_path": path,
                    "expansion": "read_decision with comparison_id returns this complete league state"}
        if all(k in value for k in ("roster", "capacity", "position_capacity", "coverage")):
            # Keep exact membership and capacity inline. The long eligibility
            # inventory is recoverable; selected lineups remain in forecasts.
            result = deepcopy(value)
            coverage = result["coverage"]
            inventory = coverage.pop("players", [])
            coverage["inventory_player_count"] = len(inventory)
            coverage["inventory_expansion_path"] = path + ["coverage", "players"]
            return result
        result = {}
        for key, child in value.items():
            if key == "original" and "experiment_id" in value:
                result[key] = {"source": "Retained experiment inputs and output",
                               "expansion_path": path + [key]}
            elif key == "strategy" and isinstance(child, dict):
                result[key] = {"source": "prepared decision context; full copy retained in comparison",
                               "expansion_path": path + [key]}
            elif key == "result" and "forecast_comparisons" in value:
                result[key] = {"source": "retained full comparison", "expansion_path": path + [key]}
            elif key == "decision_support" and isinstance(child, dict):
                # Same support is repeated at several nesting levels. Preserve
                # the first root copy, with explicit expansion for other copies.
                result[key] = deepcopy(child) if len(path) <= 3 else {"source": "retained full comparison", "expansion_path": path + [key]}
            else:
                result[key] = visit(child, path + [key])
        return result
    packet = visit(comparison, [])
    packet["detail"] = "compact"
    packet["expansion"] = {"tool": "read_decision", "decision_id": comparison["decision_id"],
                           "comparison_id": comparison["comparison_id"],
                           "scope": "Complete original snapshot and calculations retained; no refresh or rebinding"}
    return packet


def _compare(service, decision_id, actions, worlds, context):
    if not isinstance(actions, list) or not 1 <= len(actions) <= 24:
        raise DataError("Supply one to twenty-four bounded actions")
    actions = [_normalize_action(a) for a in actions]
    ids = [a.get("id") for a in actions if isinstance(a, dict)]
    if len(ids) != len(actions) or any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise DataError("Each action requires a unique nonempty string ID")
    if not any(a.get("kind") == "hold" for a in actions):
        raise DataError("Every comparison must include an explicit hold branch")
    alias, cutoff = context["alias"], context["evidence_as_of"]
    focus = set(context["specification"].get("player_ids", []))
    for action in actions:
        if not isinstance(action.get("execution_conditions", []), list):
            raise DataError("Action execution conditions must be a list")
        focus.update(_action_focus(action))
    league = service.snapshot(alias, as_of=cutoff)["data"]
    manifest = _manifest(service, alias, cutoff, league, sorted(focus))
    from .source_readiness import prepare_action_sources
    forecast_rows = service.forecast_rows(alias, cutoff)
    source_checks = {}
    result = []
    for action in actions:
        kind = action.get("kind")
        entry = {"id": action["id"], "kind": kind, "execution_conditions": deepcopy(action.get("execution_conditions", [])),
                 "championship_probability_delta": None}
        try:
            experiment_ids = action.get("experiment_ids", [])
            if not isinstance(experiment_ids, list) or len(experiment_ids) > 8 or any(
                    not isinstance(key, str) or not key for key in experiment_ids) or len(set(experiment_ids)) != len(experiment_ids):
                raise DataError("experiment_ids requires at most eight distinct saved experiment IDs")
            analyses = []
            for key in experiment_ids:
                analysis, refs = _experiment_evidence(service, context, key)
                analyses.append(analysis)
                for ref in refs:
                    if ref not in manifest:
                        manifest.append(ref)
            if analyses:
                entry["analytical_evidence"] = analyses
            if kind == "hold":
                from .lineups import packet
                entry["accounting"] = packet(service, alias, evaluation_week=action.get("week"), fresh=False,
                                               unavailable=action.get("unavailable"), as_of=cutoff)
            elif kind == "lineup":
                from .action_models import lineup_action
                entry["accounting"] = lineup_action(service, alias, action, as_of=cutoff)
            elif kind == "trade":
                entry["accounting"] = service.trade_packet(alias, action.get("proposal"), fresh=False, as_of=cutoff, expanded=True)
                entry["execution_conditions"].append("Counterparty consent is unknown")
            elif kind == "add_drop":
                from .operations import pickup_packet
                entry["accounting"] = pickup_packet(service, alias, action.get("player_id"), action.get("drop_id"),
                    evaluation_week=action.get("week"), fresh=False, expanded=True, as_of=cutoff)
            elif kind == "ordered_claims":
                from .operations import claim_scenarios
                entry["accounting"] = claim_scenarios(service, alias, action.get("claims"),
                    evaluation_week=action.get("week"), fresh=False, expanded=True, as_of=cutoff)
            elif kind == "draft_pick":
                from .action_models import draft_pick_action
                entry["accounting"] = draft_pick_action(service, alias, action, as_of=cutoff)
            elif kind == "roster_plan":
                entry["accounting"] = _roster_plan(service, context, action.get("plan", action))
                entry["execution_conditions"].append("All acquisitions/transfers are hypothetical; consent, timing and caller eligibility assertions require verification")
            else:
                raise DataError("Unsupported action kind")
            entry.setdefault("status", "mechanical_comparison")
            accounting = entry.get("accounting", {})
            mechanics = accounting.get("mechanics", accounting.get("comparison", accounting))
            if mechanics.get("valid") is False:
                entry["status"] = "blocked"
                entry["gaps"] = mechanics.get("errors") or mechanics.get("gaps") or ["Invalid action"]
        except DataError as exc:
            entry["status"] = "blocked"
            entry["gaps"] = [str(exc)]
        from .decision_readiness import describe
        action_focus = tuple(sorted(set(context["specification"].get("player_ids", [])) | _action_focus(action)))
        if action_focus not in source_checks:
            source_checks[action_focus] = prepare_action_sources(service, alias, league,
                context["specification"], forecast_rows, action_focus, as_of=cutoff)
        entry["source_readiness"] = deepcopy(source_checks[action_focus])
        entry["capability_readiness"] = describe(context, entry)
        result.append(entry)
    # Draft state is a consumed dependency too, independent of the league roster.
    for entry in result:
        board = entry.get("accounting", {}).get("draft_observation", {})
        ref = board.get("board_ref")
        if ref and ref not in manifest:
            manifest.append(ref)
    comparison = {"comparison_id": uuid.uuid4().hex, "decision_id": decision_id,
                  "revision": context["revision"], "manifest_hash": context["manifest_hash"], "actions": result, "action_specifications": deepcopy(actions),
                  "consumed_manifest": manifest, "consumed_manifest_hash": digest(manifest), "focused_player_ids": sorted(focus),
                  "worlds": deepcopy(worlds), "world_status": "uncomputed_assumptions" if worlds else "not_supplied",
                  "answer_contract": {"future_management": "Equal information timing and policy discipline; branch-specific feasible actions",
                                      "ranking": "Unweighted crossing worlds require a frontier or explicit preference, not an invented scalar winner",
                                      "flexibility": "Do not double count consequences already modeled in continuation",
                                      "championship_odds": "Not estimated by point-total accounting"}}
    return comparison


def _experiment_evidence(service, context, key):
    """Attach a saved conditional analysis, never apply it as a branch verdict.

    The host selects its relevance. Original inputs/results remain recoverable,
    and both versions become dependencies checked again when judgment is saved.
    """
    cutoff = context["evidence_as_of"]
    request = service.store.get("experiment_request", key, cutoff, required=False)
    result = service.store.get("experiment_result", key, cutoff, required=False)
    if not request or not result:
        raise DataError("Experiment was not locally available at preparation; resume before comparing")
    data = result["data"]
    league_binding = data.get("league_binding", {})
    if not league_binding.get("verified") or league_binding.get("alias") != context["alias"] or league_binding.get("snapshot_sha256") != context["snapshot"]["sha256"]:
        raise DataError("Experiment does not match this saved league/rules/roster binding")
    if data.get("status") not in ("conditional", "partial"):
        raise DataError("Blocked or unqualified experiment cannot support an action")
    if request["data"].get("binding") != data.get("binding") or timestamp(data["binding"]["as_of"]) > cutoff:
        raise DataError("Experiment request/result binding or knowability differs")
    refs = [{"kind": kind, "key": key, "version_id": record["version_id"],
             "sha256": record["sha256"], "available_at": record["available_at"]}
            for kind, record in (("experiment_request", request), ("experiment_result", result))]
    preview = {field: deepcopy(data[field][:12]) for field in ("rows", "weekly_points") if field in data}
    preview["coverage"] = {field: {"returned": min(12, len(data[field])), "total": len(data[field])}
                           for field in ("rows", "weekly_points") if field in data}
    if "branches" in data:
        preview["branch_ids"] = list(data["branches"])
        preview["requires_expansion"] = "Inspect original branches before relying on joint-weekly effects"
    return {"experiment_id": key, "module": data.get("module"), "status": data["status"],
            "method_version": data.get("method_version"), "binding": deepcopy(data["binding"]),
            "missing_inputs": deepcopy(data.get("missing_inputs", [])), "assurance": data.get("assurance"),
            "use": data.get("use"), "title_probabilities": data.get("title_probabilities"),
            "output_preview": preview,
            "evidence_refs": refs,
            "expansion": {"tool": "read_decision", "path": "actions[].analytical_evidence[].original"},
            "application": "Host-selected supporting evidence only; no automatic branch score, ranking or title-odds delta",
            "original": {"request": deepcopy(request["data"]), "result": deepcopy(data)}}, refs


def _validate_judgment_refs(service, context, comparison, judgment):
    """Check actual consumed references; existence never certifies the assertion."""
    refs = judgment.get("consulted_evidence", [])
    hits = judgment.get("claim_refs", [])
    if not isinstance(refs, list) or not isinstance(hits, list) or len(refs) > 100 or len(hits) > 100:
        raise DataError("Judgment references must be bounded lists")
    manifest = comparison["consumed_manifest"]
    for ref in refs:
        if not isinstance(ref, dict) or not all(ref.get(k) is not None for k in ("kind", "key", "version_id", "sha256")):
            raise DataError("Consulted evidence requires kind, key, version_id and sha256")
        if not any(all(ref[k] == m[k] for k in ("kind", "key", "version_id", "sha256")) for m in manifest):
            raise DataError("Consulted evidence was not consumed by this comparison; expand and recompare")
    from .evidence import EvidenceIndex
    index = EvidenceIndex(service.store) if hits else None
    validated = []
    for hit in hits:
        if isinstance(hit, bool) or not isinstance(hit, int) or hit <= 0:
            raise DataError("claim_refs require actual positive integer hit IDs")
        card = index.read(hit, as_of=context["evidence_as_of"], detail="expanded")
        if not any(m["kind"] == "evidence_document" and m["version_id"] == card["document_id"] and
                   m["sha256"] == card["audit"]["sha256"] for m in manifest):
            raise DataError("Claim document was not consumed by this comparison; expand and recompare")
        if card["player_ref"] not in {context["league_binding"]["platform"] + ":" + pid for pid in comparison["focused_player_ids"]}:
            raise DataError("Claim is outside the compared player/provider scope")
        if card["scope"] == "decision" and card["claim"].get("league_binding") != context["league_binding"]:
            raise DataError("Claim belongs to a different league binding")
        validated.append({"hit_id": hit, "document_id": card["document_id"], "sha256": card["audit"]["sha256"]})
    return {"consulted_evidence": deepcopy(refs), "claims": validated,
            "scope": "Existence and consumed dependency consistency only; not independent truth verification",
            "citation_status": "references_checked" if refs or hits else "no_references_supplied"}


def record(service, decision_id, judgment):
    epoch = service.store.version_epoch()
    context = _load(service, decision_id)
    if not isinstance(judgment, dict):
        raise DataError("Judgment must be an object")
    for field in ("comparison_id", "choice", "confidence", "strongest_countercase", "reversal_conditions"):
        if not judgment.get(field):
            raise DataError(f"Judgment requires {field}")
    comparison = service.store.get("decision_comparison", judgment["comparison_id"])["data"]
    if comparison["decision_id"] != decision_id or comparison["revision"] != context["revision"]:
        raise DataError("Comparison belongs to another decision revision; recompare after recheck")
    choices = {a["id"]: a for a in comparison["actions"]}
    if judgment["choice"] not in choices or choices[judgment["choice"]]["status"] == "blocked":
        raise DataError("Choice must reference a compared, unblocked action")
    historical = context["specification"].get("as_of") is not None
    recheck_at = context["evidence_as_of"] if historical else time.time()
    if not historical and choices[judgment["choice"]]["kind"] == "draft_pick":
        observation = choices[judgment["choice"]].get("accounting", {}).get("draft_observation", {})
        limit = observation.get("observation_max_age_seconds", 20)
        if context.get("deadline") is not None and timestamp(context["deadline"]) <= recheck_at:
            raise DataError("Draft decision deadline elapsed; recheck before using advice")
        for ref in comparison["consumed_manifest"]:
            if ref["kind"] == "draft_board":
                observed = service.store.get("draft_board", ref["key"], recheck_at)["data"].get("oldest_observed_at")
                if observed is None or recheck_at - timestamp(observed) > limit:
                    raise DataError("Consumed draft observation expired; recheck board before using advice")
    current = service.snapshot(context["alias"], as_of=recheck_at)
    if current["sha256"] != context["snapshot"]["sha256"]:
        raise DataError("Roster/rules changed since preparation; resume and compare again")
    latest = _manifest(service, context["alias"], recheck_at, current["data"], comparison["focused_player_ids"])
    for ref in comparison["consumed_manifest"]:
        if ref["kind"] in ("draft_board", "experiment_request", "experiment_result"):
            board = service.store.get(ref["kind"], ref["key"], recheck_at, required=False)
            if board:
                latest.append({"kind": ref["kind"], "key": ref["key"], "version_id": board["version_id"],
                               "sha256": board["sha256"], "available_at": board["available_at"]})
    validated_refs = _validate_judgment_refs(service, context, comparison, judgment)
    old_content = {(r["kind"], r["key"]): r["sha256"] for r in comparison["consumed_manifest"]}
    new_content = {(r["kind"], r["key"]): r["sha256"] for r in latest}
    if old_content != new_content:
        raise DataError("Consumed evidence changed since comparison; resume and compare again")
    receipt = {"decision_id": decision_id, "revision": context["revision"], "manifest_hash": context["manifest_hash"],
               "judgment": deepcopy(judgment), "validated_references": validated_refs, "recorded_at": time.time(),
               "consumed_manifest": comparison["consumed_manifest"], "recheck_manifest": latest,
               "status": "historical_host_judgment" if historical else "host_judgment_conditional", "unresolved_gaps": context["gaps"],
               "verification_scope": "Reference consistency, not truth, calibration or recommendation approval"}
    receipt["capability_readiness_by_action"] = {
        action["id"]: deepcopy(action.get("capability_readiness"))
        for action in comparison["actions"]}
    service.store.put_checked("decision_receipt", decision_id, receipt, epoch)
    return receipt


def resume(service, decision_id, refresh=True):
    old = _load(service, decision_id)
    if old["specification"].get("as_of") is not None and refresh:
        raise DataError("Historical decision cannot be refreshed with later evidence")
    previous_receipt = service.store.get("decision_receipt", decision_id, required=False)
    comparisons = [service.store.get("decision_comparison", key) for key in service.store.keys("decision_comparison")]
    comparisons = [r for r in comparisons if r["data"].get("decision_id") == decision_id and r["data"].get("revision") == old["revision"]]
    previous_comparison = max(comparisons, key=lambda r: r["version_id"])["data"] if comparisons else None
    spec = deepcopy(old["specification"])
    spec["refresh"] = refresh
    new = prepare(service, spec, decision_id=decision_id, revision=old["revision"] + 1)
    before = {(r["kind"], r["key"]): r for r in old["manifest"]}
    after = {(r["kind"], r["key"]): r for r in new["manifest"]}
    changed = [{"kind": k[0], "key": k[1], "before": before.get(k), "after": after.get(k)}
               for k in sorted(before.keys() | after.keys())
               if before.get(k, {}).get("sha256") != after.get(k, {}).get("sha256")]
    recovery = {"status": "historical_reasoning_requires_recheck",
                "judgment": previous_receipt["data"]["judgment"] if previous_receipt else None,
                "judgment_revision": previous_receipt["data"]["revision"] if previous_receipt else None,
                "comparison_id": previous_comparison["comparison_id"] if previous_comparison else None,
                "alternatives": [{k: a.get(k) for k in ("id", "kind", "status", "execution_conditions", "gaps", "capability_readiness")}
                                 for a in previous_comparison["actions"]] if previous_comparison else [],
                "action_specifications": previous_comparison.get("action_specifications") if previous_comparison else None,
                "worlds": previous_comparison.get("worlds") if previous_comparison else None,
                "expansion": "Load retained comparison by comparison_id for full mechanical detail; prior judgment is not rebound automatically"}
    return {"decision_id": decision_id, "previous_revision": old["revision"], "context": new, "recovered_reasoning": recovery,
            "changed_resources": changed, "comparisons_require_recheck": True,
            "content_changed": bool(changed), "reason": "New revision requires explicitly rebound comparisons even when content is unchanged",
            "capsule": {**{k: new[k] for k in ("decision_id", "revision", "question", "objective", "horizon", "deadline",
                                             "league_binding", "rules_hash", "manifest_hash", "gaps", "expansion", "capability_readiness")}, "recovered_reasoning": recovery}}


def read_saved(service, decision_id, comparison_id=None):
    """Recover the original artifact without fetching or rebinding its evidence."""
    context = _load(service, decision_id)
    receipt = service.store.get("decision_receipt", decision_id, required=False)
    compared = service.store.get("decision_comparison", comparison_id)["data"] if comparison_id else None
    if compared and compared["decision_id"] != decision_id:
        raise DataError("Comparison belongs to another decision")
    return {"context": context, "receipt": receipt["data"] if receipt else None,
            "comparison": compared, "recheck_required": True,
            "scope": "Retained reasoning and evidence only; this read neither refreshes nor certifies current advice"}
