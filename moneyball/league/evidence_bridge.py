"""Project preserved dossier versions into reusable evidence without changing history.

This is an index migration of locally stored summaries, not new article capture,
source verification, or permission to redistribute the underlying publications.
"""
from copy import deepcopy
import json

from .evidence import EvidenceIndex
from .store import DataError, digest, timestamp

SCHEMA_VERSION = "dossier-evidence-bridge-v1"


def _strings(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [x for x in value if isinstance(x, str)]
    return []


def _document(player_ref, record):
    data, receipt = record["data"], {"version_id": record["version_id"], "key": player_ref, "sha256": record["sha256"]}
    sections, claims, gaps = [], [], list(_strings(data.get("open_questions")))
    full = data.get("full_review")
    if isinstance(full, str) and full:
        sections.append({"id": "full_review", "text": full, "locator": "Store dossier.full_review"})
    original_excerpts = (data.get("legacy_evidence") or {}).get("review_excerpts", {})
    original_claims = data.get("claims", [])
    if not isinstance(original_claims, list):
        gaps.append("Malformed claims collection omitted")
        original_claims = []
    for i, row in enumerate(original_claims):
        if isinstance(row, str):
            row = {"text": row}
        if not isinstance(row, dict):
            gaps.append("Unstructured claim omitted at index " + str(i))
            continue
        text = row.get("text") or row.get("statement") or row.get("claim")
        section_name = row.get("section")
        if row.get("truncated") and isinstance(original_excerpts.get(section_name), str):
            text = original_excerpts[section_name]
        if not isinstance(text, str) or not text.strip():
            gaps.append("Claim without inspectable text omitted at index " + str(i))
            continue
        scope = row.get("scope", "decision" if row.get("league_binding") else "shared")
        if scope not in ("shared", "decision"):
            gaps.append("Claim omitted because its scope is unknown")
            continue
        if scope == "decision" and not isinstance(row.get("league_binding"), dict):
            gaps.append("League-specific claim omitted because its exact binding is missing")
            continue
        qualifiers = _strings(row.get("qualifiers"))
        if row.get("truncated") and text == row.get("text"):
            qualifiers.append("Preserved excerpt is truncated; expand its full parent before relying")
        if data.get("archive_import"):
            qualifiers.append("Archived dynasty analysis: observations may transfer; old valuations and strategic effects require current rule/roster assessment")
        kind = row.get("kind", "interpretation")
        if kind not in ("observation", "interpretation", "assumption", "forecast"):
            qualifiers.append("Original claim kind was not recognized: " + str(kind))
            kind = "interpretation"
        parent_id = "full_review" if sections and sections[0]["id"] == "full_review" else "claim-" + str(i)
        if parent_id != "full_review":
            parent = row.get("parent_text", text)
            sections.append({"id": parent_id, "text": parent if isinstance(parent, str) else text,
                             "locator": row.get("parent_locator", "Store dossier.claims[" + str(i) + "]")})
        ident = str(row.get("id") or "claim-" + str(i))
        if any(c["id"] == ident for c in claims):
            ident += "-" + str(i)
        claim = {"id": ident, "text": text, "player_ref": player_ref,
                 "facet": str(row.get("facet") or section_name or "unspecified"),
                 "parent_id": parent_id, "kind": kind,
                 "scope": scope, "qualifiers": qualifiers,
                 "counterevidence": _strings(row.get("counterevidence")),
                 "reversal_conditions": _strings(row.get("reversal_conditions")) + _strings(data.get("reversal_conditions")),
                 "locator": "Store dossier version " + str(record["version_id"]) + "/claims/" + str(i)}
        if scope == "decision":
            claim["league_binding"] = deepcopy(row["league_binding"])
        claims.append(claim)
    if not data.get("sources"):
        gaps.append("Preserved dossier has no attributed source records")
    return {"source": {"url": "store:dossier:" + player_ref, "origin": "preserved_local_dossier",
                       "capture_policy": "permitted_excerpt", "original_sources": deepcopy(data.get("sources", [])),
                       "rights_status": "Existing local summaries indexed; underlying publication rights not independently verified"},
            "sections": sections, "claims": claims, "preserved_store_version": receipt,
            "evidence_checked_at": data.get("evidence_checked_at"),
            "coverage": {"status": "partial", "gaps": gaps, "scope": "Preserved dossier claims only; no completeness or freshness inferred"},
            "schema_version": SCHEMA_VERSION}


def migrate_dossiers(store, player_refs, *, as_of=None):
    """Migration is bound to actual immutable Store version IDs and their dates."""
    if store.in_read_view:
        raise DataError("Prepare dossier indexing before opening a decision read view")
    cutoff, index, migrated = timestamp(as_of), EvidenceIndex(store), []
    for ref in player_refs:
        with store.db() as db:
            rows = db.execute("SELECT id,available_at,hash,body FROM versions WHERE kind='dossier' AND key=? AND available_at<=? ORDER BY available_at,id", (ref, cutoff)).fetchall()
        for row in rows:
            data = json.loads(row[3])
            if digest(data) != row[2]:
                raise DataError("Preserved dossier integrity failure")
            record = {"version_id": row[0], "available_at": row[1], "sha256": row[2], "data": data}
            receipt = index._index_document("dossier:" + ref, _document(ref, record),
                                            preserved_record={"version_id": row[0], "key": ref})
            migrated.append(receipt)
    return migrated


def evidence_for_decision(store, league, focus, asof=None, *, detail="decisive", limit=12):
    """Get focused exact-provider evidence plus consumed dossier manifests.

    Returns indexed_cards, manifest_refs, index_manifest, schema_signature,
    coverage/gaps. The parent decision manifest must consume both dossier refs
    and independent index_manifest revision IDs when standalone claims are used.
    """
    if not isinstance(focus, (list, tuple)) or len(focus) > 12:
        raise DataError("Decision evidence supports at most twelve focused player IDs")
    cutoff = timestamp(asof)
    platform = league["platform"]
    refs = [platform + ":" + str(pid) for pid in dict.fromkeys(focus)]
    if not store.in_read_view:
        migrate_dossiers(store, refs, as_of=cutoff)
    binding = {key: league[key] for key in ("platform", "league_id", "season") if key in league}
    if "rules_hash" in league:
        binding["rules_hash"] = league["rules_hash"]
    index = EvidenceIndex(store)
    page = index.search(player_refs=refs, as_of=cutoff, league_binding=binding, detail=detail, limit=limit) if refs else {"items": [], "next_cursor": None, "total_matching": 0}
    manifest, missing = [], []
    for ref in refs:
        record = store.get("dossier", ref, cutoff, required=False)
        if record:
            manifest.append({"kind": "dossier", "key": ref, "version_id": record["version_id"], "sha256": record["sha256"], "available_at": record["available_at"]})
        elif not any(c["player_ref"] == ref for c in page["items"]):
            missing.append(ref)
    index_manifest = index.manifest(refs, as_of=cutoff, league_binding=binding) if refs else []
    return {"indexed_cards": page["items"], "next_cursor": page.get("next_cursor"), "matching_claim_count": page["total_matching"],
            "manifest_refs": manifest, "index_manifest": index_manifest, "schema_signature": digest({"bridge": SCHEMA_VERSION, "index": "evidence-fts-v1"}),
            "coverage": {"status": "partial", "requested_player_refs": refs, "missing_player_refs": missing,
                         "candidate_scope": "Exact requested providers/players; no automatic crosswalk or comprehensive evidence claim"},
            "gaps": (["No preserved/indexed evidence for " + ref for ref in missing] +
                     (["Additional matching claims require next_cursor expansion"] if page.get("next_cursor") else [])),
            "content_role": "untrusted_evidence", "as_of": cutoff}


def index_manifest_for_decision(store, league, focus, asof=None):
    """Read consumed-key revisions without migrating or changing evidence."""
    refs = [league["platform"] + ":" + str(pid) for pid in dict.fromkeys(focus)]
    binding = {key: league[key] for key in ("platform", "league_id", "season", "rules_hash") if key in league}
    with store._read_db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='evidence_documents'").fetchone():
            return []
    return EvidenceIndex(store).manifest(refs, as_of=asof, league_binding=binding)
