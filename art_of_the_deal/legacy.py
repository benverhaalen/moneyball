"""Import preserved Moneyball draft dossiers as explicitly stale evidence.

The importer does not reinterpret an old review.  It preserves the review,
structured source receipts, identities, and original timestamps while making
the local import time a separate fact.  Nothing in this module performs a
network request or a fuzzy identity match.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .store import DataError, digest, encode, timestamp


MAX_DECK_BYTES = 50 * 1024 * 1024
MAX_CARDS = 5_000
ARCHIVE_KIND = "moneyball_draft_deck"


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise DataError(field + " must be a nonempty string")
    return value


def _finite_timestamp(value: Any, field: str) -> float:
    try:
        result = timestamp(value)
    except (TypeError, ValueError) as exc:
        raise DataError(field + " must be a finite timestamp") from exc
    return result


def _compact_exact(text: str, limit: int = 1_600) -> dict[str, Any]:
    """Return a bounded, visibly truncated copy of existing review prose."""
    if len(text) <= limit:
        return {"text": text, "truncated": False}
    return {"text": text[:limit].rstrip(), "truncated": True,
            "full_text_location": "full_review"}


def _load_review_sidecar(card: dict[str, Any], deck_path: Path) -> dict[str, Any] | None:
    review = card.get("review")
    if review is None:
        return None
    if not isinstance(review, dict):
        raise DataError("card.review must be an object or null")
    evidence_path = review.get("evidence_path")
    if evidence_path is None:
        return None
    evidence_path = Path(_text(evidence_path, "card.review.evidence_path")).expanduser()
    if not evidence_path.is_absolute():
        evidence_path = deck_path.parent / evidence_path
    evidence_path = evidence_path.resolve()
    try:
        raw = evidence_path.read_bytes()
    except OSError as exc:
        raise DataError("Referenced reviewed evidence is unavailable: " + str(evidence_path)) from exc
    expected = _text(review.get("evidence_sha256"), "card.review.evidence_sha256")
    actual = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        raise DataError("Reviewed evidence hash mismatch: " + str(evidence_path))
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError("Reviewed evidence is not valid JSON: " + str(evidence_path)) from exc
    if not isinstance(value, dict):
        raise DataError("Reviewed evidence must be a JSON object")
    return value


def _source_rows(card: dict[str, Any], reviewed: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Retain structured source records already present; invent no source claims."""
    rows: list[dict[str, Any]] = []

    if reviewed is not None:
        primary = reviewed.get("primary_sources", [])
        if not isinstance(primary, list):
            raise DataError("reviewed primary_sources must be a list")
        for source in primary:
            if not isinstance(source, dict) or not source.get("url"):
                raise DataError("Each reviewed primary source must be an object with a URL")
            rows.append({"origin_section": "reviewed.primary_sources", "record": deepcopy(source)})

    forecasts = card.get("forecasts", [])
    if not isinstance(forecasts, list):
        raise DataError("card.forecasts must be a list")
    for source in forecasts:
        if not isinstance(source, dict):
            raise DataError("Each card forecast must be an object")
        if source.get("source_url"):
            rows.append({"origin_section": "card.forecasts", "record": deepcopy(source)})

    markets = card.get("markets", [])
    if not isinstance(markets, list):
        raise DataError("card.markets must be a list")
    for market in markets:
        if not isinstance(market, dict):
            raise DataError("Each card market must be an object")
        action = market.get("action_rules") or {}
        observations = market.get("paired_observations") or []
        has_url = bool(market.get("source_url"))
        has_url = has_url or (isinstance(action, dict) and bool(action.get("url")))
        has_url = has_url or (isinstance(observations, list) and any(
            isinstance(row, dict) and row.get("source_url") for row in observations
        ))
        if has_url:
            rows.append({"origin_section": "card.markets", "record": deepcopy(market)})

    contracts = card.get("contracts")
    if contracts is not None and not isinstance(contracts, dict):
        raise DataError("card.contracts must be an object or null")
    if isinstance(contracts, dict) and contracts.get("source_url"):
        rows.append({"origin_section": "card.contracts", "record": deepcopy(contracts)})

    # Some sections repeat the same exact structured record.  Remove only exact
    # duplicates; distinct observations from one URL remain distinct evidence.
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = digest(row)
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


def _review_time(card: dict[str, Any], reviewed: dict[str, Any] | None) -> float:
    candidates: list[float] = []
    review = card.get("review") or {}
    for label, value in (
        ("card.review.available_at", review.get("available_at")),
        ("reviewed.available_at", reviewed.get("available_at") if reviewed else None),
        ("reviewed.known_at", reviewed.get("known_at") if reviewed else None),
    ):
        if value is not None:
            candidates.append(_finite_timestamp(value, label))
    if not candidates:
        return _finite_timestamp(card.get("information_cutoff"), "card.information_cutoff")
    return max(candidates)


def _dossier(
    card_key: str,
    card: dict[str, Any],
    reviewed: dict[str, Any] | None,
    deck: dict[str, Any],
    deck_path: Path,
    deck_file_sha256: str,
    imported_at: float,
) -> dict[str, Any]:
    excerpts = card.get("review_excerpts")
    if not isinstance(excerpts, dict):
        raise DataError("card.review_excerpts must be an object")
    full_review = _text(card.get("full_review"), "card.full_review", allow_empty=True)
    original_cutoff = _finite_timestamp(card.get("information_cutoff"), "card.information_cutoff")
    evidence_checked_at = _review_time(card, reviewed)
    if evidence_checked_at < original_cutoff:
        raise DataError("Review evidence time precedes its information cutoff")

    claims = []
    for section in ("opening", "future", "risk"):
        value = excerpts.get(section)
        if value is not None:
            claims.append({"section": section, **_compact_exact(_text(value, "review_excerpts." + section))})

    reversals = []
    if excerpts.get("reversal"):
        reversals.append(_text(excerpts["reversal"], "review_excerpts.reversal"))
    if reviewed and reviewed.get("reversal_test") and reviewed["reversal_test"] not in reversals:
        reversals.append(_text(reviewed["reversal_test"], "reviewed.reversal_test"))

    identity = card["identity"]
    espn_id = identity.get("external_ids", {}).get("espn_id")
    refs = ["sleeper:" + card_key] + (["espn:" + str(espn_id)] if espn_id is not None else [])
    opening = excerpts.get("opening") or ""
    summary_excerpt = _compact_exact(opening, 600)
    card_sha256 = digest(card)
    origin_handle = "moneyball-deck:" + deck["content_hash"] + ":sleeper:" + card_key

    return {
        "schema_version": 1,
        "summary": {
            "name": card["name"],
            "position": card["position"],
            "classification": "archival_dynasty_draft_dossier",
            "review_status": (card.get("review") or {}).get("status"),
            "excerpt": summary_excerpt,
            "freshness": "refresh_required_before_current_role_or_availability_advice",
        },
        "claims": claims,
        "sources": _source_rows(card, reviewed),
        "evidence_checked_at": evidence_checked_at,
        "reversal_conditions": reversals,
        "full_review": full_review,
        "identity": {
            "canonical_ref": "sleeper:" + card_key,
            "exact_player_refs": refs,
            "sleeper_id": card_key,
            "espn_id": str(espn_id) if espn_id is not None else None,
            "name_at_review": card["name"],
            "position_at_review": card["position"],
            "identity_match": deepcopy(identity.get("identity_match", [])),
        },
        "archive_import": {
            "kind": ARCHIVE_KIND,
            "local_origin_handle": origin_handle,
            "local_origin_path": str(deck_path),
            "source_deck_content_hash": deck["content_hash"],
            "source_deck_file_sha256": deck_file_sha256,
            "source_card_sha256": card_sha256,
            "packet_build": deck.get("packet_build"),
            "deck_built_at": _finite_timestamp(deck.get("built_at"), "deck.built_at"),
            "original_information_cutoff": original_cutoff,
            "original_review_available_at": evidence_checked_at,
            "locally_imported_at": imported_at,
            "freshness_status": "archival_not_current",
            "refresh_recommendation": (
                "Preserve structural analysis; refresh volatile role, availability, projections, "
                "team context, market observations, and league-specific use before advice."
            ),
        },
        "legacy_review_record": deepcopy(card.get("review")),
        "legacy_reviewed_evidence": deepcopy(reviewed),
        "legacy_evidence": {
            "identity": deepcopy(identity),
            "forecasts": deepcopy(card.get("forecasts", [])),
            "markets": deepcopy(card.get("markets", [])),
            "contracts": deepcopy(card.get("contracts")),
            "coverage": deepcopy(card.get("coverage")),
            "review_excerpts": deepcopy(excerpts),
            "calibrated_title_delta": deepcopy(card.get("calibrated_title_delta")),
            "deck_policy": deepcopy(deck.get("policy")),
            "packet_path": card.get("packet_path"),
            "packet_sha256": card.get("packet_sha256"),
        },
        "interpretation": (
            "Imported archival evidence, not a current recommendation. Original review and "
            "information cutoffs are preserved separately from local availability."
        ),
    }


def import_moneyball_deck(service: Any, path: str | Path) -> dict[str, Any]:
    """Import an explicit Moneyball deck into ``service.store`` idempotently.

    The deck's declared content hash and every referenced reviewed-evidence hash
    are checked before any dossier version is written.  Exact Sleeper IDs are
    canonical; exact embedded ESPN IDs receive an alias dossier.  Existing
    non-legacy dossiers are protected from being displaced by old evidence.
    """
    if service is None or not hasattr(service, "store"):
        raise DataError("A Service with a private store is required")
    if path is None:
        raise DataError("Supply an explicit local Moneyball deck path")
    deck_path = Path(path).expanduser().resolve()
    if not deck_path.is_file():
        raise DataError("Moneyball deck path is not a readable file: " + str(deck_path))
    size = deck_path.stat().st_size
    if size <= 0 or size > MAX_DECK_BYTES:
        raise DataError("Moneyball deck must be nonempty and at most 50 MiB")
    try:
        raw = deck_path.read_bytes()
        deck = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataError("Moneyball deck is not readable valid JSON") from exc
    if not isinstance(deck, dict) or deck.get("schema_version") != 1:
        raise DataError("Unsupported Moneyball deck schema")
    cards = deck.get("cards")
    if not isinstance(cards, dict) or not cards or len(cards) > MAX_CARDS:
        raise DataError("Moneyball deck cards must be a nonempty bounded object")
    for key in ("packet_build", "policy", "content_hash", "built_at"):
        if key not in deck:
            raise DataError("Moneyball deck requires " + key)
    expected_hash = digest({key: deck[key] for key in ("packet_build", "policy", "cards")})
    if deck.get("content_hash") != expected_hash:
        raise DataError("Moneyball deck content hash mismatch")
    deck_file_sha256 = hashlib.sha256(raw).hexdigest()
    imported_at = time.time()

    seen_sleeper: set[str] = set()
    seen_espn: set[str] = set()
    prepared: list[tuple[list[str], dict[str, Any]]] = []
    for raw_key, raw_card in cards.items():
        key = _text(str(raw_key), "card key")
        if not isinstance(raw_card, dict):
            raise DataError("Every Moneyball card must be an object")
        card = raw_card
        player_id = _text(str(card.get("player_id")), "card.player_id")
        if key != player_id:
            raise DataError("Card key and Sleeper player_id must match exactly")
        if key in seen_sleeper:
            raise DataError("Duplicate Sleeper player ID in Moneyball deck: " + key)
        seen_sleeper.add(key)
        _text(card.get("name"), "card.name")
        _text(card.get("position"), "card.position")
        identity = card.get("identity")
        if not isinstance(identity, dict) or str(identity.get("player_id")) != key:
            raise DataError("Card identity must contain the exact Sleeper player ID")
        if identity.get("name") != card.get("name"):
            raise DataError("Card and identity names must match exactly")
        external = identity.get("external_ids", {})
        if not isinstance(external, dict):
            raise DataError("identity.external_ids must be an object")
        espn = external.get("espn_id")
        refs = ["sleeper:" + key]
        if espn is not None:
            if isinstance(espn, bool) or not isinstance(espn, (str, int)) or not str(espn):
                raise DataError("ESPN ID must be a nonempty exact string or integer")
            espn = str(espn)
            if espn in seen_espn:
                raise DataError("Duplicate ESPN player ID in Moneyball deck: " + espn)
            seen_espn.add(espn)
            refs.append("espn:" + espn)

        reviewed = _load_review_sidecar(card, deck_path)
        if reviewed is not None:
            if str(reviewed.get("player_id")) != key:
                raise DataError("Reviewed evidence identity does not exactly match its card")
            # Some later reviewed manifests intentionally contain no duplicated
            # name field.  Absence is not a second identity observation; when a
            # name is present it must match exactly.
            if reviewed.get("name") is not None and reviewed.get("name") != card.get("name"):
                raise DataError("Reviewed evidence identity does not exactly match its card")
            packet = reviewed.get("packet") or {}
            if packet.get("information_cutoff") is not None and (
                _finite_timestamp(packet["information_cutoff"], "reviewed.packet.information_cutoff")
                != _finite_timestamp(card.get("information_cutoff"), "card.information_cutoff")
            ):
                raise DataError("Reviewed evidence and card information cutoffs differ")
        prepared.append((refs, _dossier(
            key, card, reviewed, deck, deck_path, deck_file_sha256, imported_at
        )))

    pending: list[tuple[str, dict[str, Any]]] = []
    skipped = 0
    protected = 0
    first_import_times: list[float] = []
    for refs, dossier in prepared:
        exact_existing = None
        for ref in refs:
            saved = service.store.get("dossier", ref, required=False)
            if not saved:
                continue
            archive = saved["data"].get("archive_import", {})
            same_card = (
                archive.get("kind") == ARCHIVE_KIND
                and archive.get("source_deck_content_hash") == deck["content_hash"]
                and archive.get("source_card_sha256") == dossier["archive_import"]["source_card_sha256"]
            )
            if same_card:
                exact_existing = saved["data"]
                skipped += 1
                value = archive.get("locally_imported_at")
                if isinstance(value, (int, float)):
                    first_import_times.append(float(value))
                continue
            if archive.get("kind") != ARCHIVE_KIND:
                protected += 1
                raise DataError("Refusing to displace a non-legacy dossier at " + ref)
        for ref in refs:
            saved = service.store.get("dossier", ref, required=False)
            archive = saved["data"].get("archive_import", {}) if saved else {}
            if saved and archive.get("source_deck_content_hash") == deck["content_hash"] and (
                archive.get("source_card_sha256") == dossier["archive_import"]["source_card_sha256"]
            ):
                continue
            pending.append((ref, deepcopy(exact_existing or dossier)))

    if pending:
        provenance = {
            "scope": "archival Moneyball dossier; not current advice",
            "origin_path": str(deck_path),
            "origin_content_hash": deck["content_hash"],
            "origin_file_sha256": deck_file_sha256,
            "locally_available_at": imported_at,
        }
        # One database transaction prevents a malformed or interrupted import
        # from publishing only part of a validated deck.
        with service.store.db() as db:
            for ref, dossier in pending:
                body = encode(dossier)
                db.execute(
                    "INSERT INTO versions(kind,key,available_at,hash,body,provenance) VALUES (?,?,?,?,?,?)",
                    ("dossier", ref, imported_at, digest(dossier), body, encode(provenance)),
                )
        service.store.blob(raw)

    return {
        "status": "imported" if pending else "already_imported",
        "classification": "archival_not_current",
        "deck_path": str(deck_path),
        "deck_content_hash": deck["content_hash"],
        "deck_file_sha256": deck_file_sha256,
        "cards_validated": len(prepared),
        "sleeper_identities": len(seen_sleeper),
        "espn_identities": len(seen_espn),
        "dossier_revisions_written": len(pending),
        "existing_aliases_skipped": skipped,
        "protected_existing_dossiers": protected,
        "locally_imported_at": min(first_import_times) if not pending and first_import_times else imported_at,
        "original_deck_built_at": _finite_timestamp(deck["built_at"], "deck.built_at"),
        "freshness": "Refresh current role, availability, forecasts, and team context before advice.",
    }
