"""Bind calling-agent research to observed rules; never impersonate a researcher.

Connection prepares work automatically. The host agent reads primary sources,
reasons, and submits a report. Validation establishes structure and binding,
not source truth, strategy quality or a predictive edge.
"""
from copy import deepcopy
from importlib.resources import files
import time
from urllib.parse import urlparse

from .store import DataError, digest, timestamp

METHOD_VERSION = "moneyball-league-research-v2"
OPERATIONS = ("draft", "lineup", "waiver", "trade")


def skill_text(reference=None):
    root = files("art_of_the_deal").joinpath("skills/moneyball-strategy")
    if reference not in (None, "research-method", "trade-evaluation"):
        raise DataError("Unknown skill reference")
    path = root.joinpath("references", reference + ".md") if reference else root.joinpath("SKILL.md")
    return path.read_text()


def identity(league):
    return {
        "method_version": METHOD_VERSION,
        "rules_fingerprint": league["strategy_key"],
        "platform": league["platform"], "league_id": str(league["league_id"]),
        "season": league["season"], "own_team_id": league.get("own_team_id"),
        "team_count": len(league["teams"]),
    }


def request(league, profile):
    binding = identity(league)
    return {
        "research_key": digest(binding), "binding": binding,
        "status": "pending_agent_research",
        "objective": "Improve this team's championship prospects through legal draft, lineup, waiver and trade decisions over its actual ownership horizon.",
        "league_inputs": {"format": league["format"], "rules": deepcopy(league["rules"]),
                          "team_count": len(league["teams"])},
        "missing_rules": profile["missing_rules"],
        "compiled_mechanisms": profile["mechanisms"],
        "workflow": [
            "Freeze exact rules and unresolved fields. Read Moneyball's packaged research-method reference.",
            "Read the packaged Art of the Deal trade-evaluation reference. Establish hold versus trade with equally feasible future management, full legal lineup substitutions for both teams, outside options and service windows before writing the trade policy.",
            "Express the constraints without fantasy terminology; discover direct and structurally distant methods that could change a decision.",
            "Inspect primary sources; record access scope and counterevidence. Existing library references are leads, not proof of a new reading.",
            "Map each candidate to exact rule paths, assumptions, differences, failure conditions and a discriminating comparison.",
            "Synthesize conditional policies for draft, lineup, waiver and trade; retain rejected transfers and research gaps.",
            "Save the report with its research_key. Reuse stable reasoning while refreshing dynamic evidence separately.",
        ],
        "skill": {"resource": "moneyball://skills/strategy", "cli": "moneyball-agent skill"},
        "method_resources": ["moneyball://research/method", "moneyball://research/trades"],
        "completion_tool": "save_league_research",
        "completion_cli": "moneyball-agent research-save ALIAS REPORT.json",
        "report_fields": ["research_key", "summary", "sources", "mechanisms", "policies", "rejected_transfers", "research_gaps"],
        "execution": "The connected coding agent performs research with its available browsing tools. This server makes no model calls or background research claims.",
    }


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DataError(label + " must be nonempty text")


def _texts(value, label, *, required=True):
    if not isinstance(value, list) or (required and not value):
        raise DataError(label + " must be a list" + (" with at least one item" if required else ""))
    for item in value:
        _text(item, label + " item")


def _rule_value(inputs, path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise DataError("rule_paths must be JSON pointers into league_inputs")
    value = inputs
    try:
        for part in path[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, IndexError, TypeError, ValueError):
        raise DataError("Unknown rule evidence path: " + path) from None
    return deepcopy(value)


def validate_report(report, plan):
    if not isinstance(report, dict):
        raise DataError("Research report must be an object")
    if report.get("research_key") != plan["research_key"]:
        raise DataError("Research inputs changed; retrieve the current research plan before saving")
    _text(report.get("summary"), "summary")
    sources = report.get("sources")
    if not isinstance(sources, list) or not sources:
        raise DataError("Record inspected primary sources; a compiled profile is not fresh research")
    source_ids = set()
    for source in sources:
        if not isinstance(source, dict):
            raise DataError("Each source must be an object")
        for field in ("id", "title", "url", "inspected_scope"):
            _text(source.get(field), "source." + field)
        if source["id"] in source_ids:
            raise DataError("Duplicate source ID")
        source_ids.add(source["id"])
        parsed = urlparse(source["url"])
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise DataError("Source URL must be a public HTTP(S) reference without credentials")
        if source.get("access") not in ("full_text", "excerpt", "abstract"):
            raise DataError("Supporting sources require explicit full_text, excerpt or abstract access")
        checked = timestamp(source.get("checked_at")) if source.get("checked_at") is not None else None
        if checked is None or checked > time.time() + 60:
            raise DataError("Source checked_at must be an actual, nonfuture reading time")
    mechanisms = report.get("mechanisms")
    if not isinstance(mechanisms, list) or not mechanisms:
        raise DataError("Record at least one conditional mechanism")
    mechanism_ids = set()
    mechanism_status = {}
    result = deepcopy(report)
    for mechanism in result["mechanisms"]:
        if not isinstance(mechanism, dict):
            raise DataError("Each mechanism must be an object")
        for field in ("id", "domain", "structural_match", "transfer"):
            _text(mechanism.get(field), "mechanism." + field)
        if mechanism["id"] in mechanism_ids:
            raise DataError("Duplicate mechanism ID")
        mechanism_ids.add(mechanism["id"])
        for field in ("rule_paths", "source_ids", "assumptions", "failure_conditions", "evidence_needed"):
            _texts(mechanism.get(field), "mechanism." + field)
        if not set(mechanism["source_ids"]) <= source_ids:
            raise DataError("Mechanism cites an unrecorded source")
        mechanism["rule_evidence"] = [{"path": p, "value": _rule_value(plan["league_inputs"], p)}
                                      for p in mechanism["rule_paths"]]
        test = mechanism.get("test")
        if not isinstance(test, dict):
            raise DataError("Mechanism needs a discriminating test")
        for field in ("comparison", "observable_outcome"):
            _text(test.get(field), "test." + field)
        if mechanism.get("status") not in ("candidate", "adopted", "deferred", "contradicted"):
            raise DataError("Mechanism status must distinguish candidate, adopted, deferred or contradicted")
        mechanism_status[mechanism["id"]] = mechanism["status"]
    policies = result.get("policies")
    if not isinstance(policies, dict) or set(policies) != set(OPERATIONS):
        raise DataError("Policies must cover draft, lineup, waiver and trade")
    for operation, policy in policies.items():
        if not isinstance(policy, dict):
            raise DataError("Each policy must be an object")
        _text(policy.get("reasoning"), operation + ".reasoning")
        _texts(policy.get("mechanism_ids"), operation + ".mechanism_ids", required=False)
        if not set(policy["mechanism_ids"]) <= mechanism_ids:
            raise DataError("Policy cites an unrecorded mechanism")
        if any(mechanism_status[mid] in ("contradicted", "deferred") for mid in policy["mechanism_ids"]):
            raise DataError("An operative policy cannot rely on contradicted or deferred mechanisms")
        for field in ("evidence_needed", "reversal_conditions"):
            _texts(policy.get(field), operation + "." + field)
    _texts(result.get("research_gaps"), "research_gaps", required=False)
    rejected = result.get("rejected_transfers")
    if not isinstance(rejected, list):
        raise DataError("rejected_transfers must be a list")
    for transfer in rejected:
        if not isinstance(transfer, dict):
            raise DataError("Rejected transfer must be an object")
        for field in ("mechanism", "reason", "revisit_when"):
            _text(transfer.get(field), "rejected_transfer." + field)
    result["binding"] = deepcopy(plan["binding"])
    result["missing_rules"] = deepcopy(plan["missing_rules"])
    result["assurance"] = "Caller-authored research; schema and league binding checked, source reading and strategic merit not independently certified. No measured predictive edge follows."
    # Fail before storage for non-JSON or nonfinite data, including extra fields.
    digest(result)
    return result


def summary(store, league, as_of=None):
    historical = as_of is not None and league.get("research_key")
    key = league["research_key"] if historical else digest(identity(league))
    record = store.get("league_research", key, as_of, required=False)
    base = {"research_key": key, "method_version": league["research_method_version"] if historical else METHOD_VERSION}
    if not record:
        return {**base, "status": "pending_agent_research", "next_tool": "league_research_plan",
                "instruction": "Complete the packaged Moneyball strategy research workflow, then save_league_research. Do not label the compiled rules as completed research."}
    data = record["data"]
    return {**base, "status": "saved_agent_research", "summary": data["summary"],
            "available_at": record["available_at"], "sha256": record["sha256"],
            "policies": data["policies"], "research_gaps": data["research_gaps"],
            "mechanism_conditions": [{k: m[k] for k in ("id", "status", "assumptions", "failure_conditions", "rule_evidence")}
                                     for m in data["mechanisms"] if m["status"] in ("candidate", "adopted")],
            "missing_rules": data["missing_rules"], "assurance": data["assurance"]}
