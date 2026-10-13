"""Reusable evidence, conservative knowability and whole-claim FTS5 retrieval.

SQLite FTS5 (https://sqlite.org/fts5.html, public domain) supplies the index.
Parent-section retrieval is implemented locally without framework dependencies.
Source text is untrusted evidence; retrieval never verifies truth or entailment.
"""
import base64
import binascii
import hashlib
from copy import deepcopy
import json
import re
import sqlite3
import time

from .store import DataError, digest, encode, timestamp


class EvidenceIndex:
    """An additive index in Store's database, with atomic immutable revisions."""
    def __init__(self, store):
        self.store = store
        with store._read_db() as db:
            initialized = db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN ('evidence_documents','evidence_claims','evidence_fts')").fetchone()[0] == 3
        if initialized:
            return
        if store.in_read_view:
            raise DataError("Initialize evidence indexing before opening a read view")
        with store.db() as db:
            try:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS evidence_documents (
                  id INTEGER PRIMARY KEY, document_key TEXT NOT NULL, observed_at REAL NOT NULL,
                  hash TEXT NOT NULL, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evidence_document_versions
                  ON evidence_documents(document_key,observed_at,id);
                CREATE TABLE IF NOT EXISTS evidence_claims (
                  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL,
                  claim_key TEXT NOT NULL, player_ref TEXT NOT NULL, facet TEXT NOT NULL,
                  scope TEXT NOT NULL, binding TEXT, body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS evidence_player_lookup ON evidence_claims(player_ref,document_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(text, parent_text, tokenize='unicode61');
                """)
            except sqlite3.OperationalError as exc:
                raise DataError("Evidence text search requires an SQLite runtime with FTS5 enabled") from exc

    def index_document(self, key, document):
        return self._index_document(key, document)

    def _index_document(self, key, document, *, preserved_record=None):
        """Import permitted text now; supplied publication dates never backdate it.

        Document: source{url,origin,capture_policy}, sections[{id,text,locator}],
        claims[{id,player_ref,facet,text,parent_id,kind,qualifiers,counterevidence,
                reversal_conditions,scope,league_binding,locator}], coverage.
        capture_policy is link_only/permitted_excerpt/full_authorized. Original
        capture is an optional existing Store blob hash, never implicit scraping.
        """
        if not isinstance(key, str) or not key.strip():
            raise DataError("A nonempty evidence document key is required")
        data = deepcopy(document)
        if not isinstance(data, dict):
            raise DataError("Evidence document must be an object")
        source = data.get("source")
        if not isinstance(source, dict) or not source.get("url") or not source.get("origin"):
            raise DataError("Evidence source requires URL/locator and origin")
        policy = source.get("capture_policy")
        if policy not in ("link_only", "permitted_excerpt", "full_authorized"):
            raise DataError("Explicit source capture policy is required")
        if source.get("raw_sha256"):
            if policy != "full_authorized":
                raise DataError("Original captures require full_authorized capture policy")
            sha = source["raw_sha256"]
            if not re.fullmatch(r"[a-f0-9]{64}", str(sha)) or not (self.store.blobs / sha).is_file():
                raise DataError("Original capture must reference an existing Store blob")
        for field in ("event_at", "published_at", "retrieved_at"):
            if data.get(field) is not None:
                data[field] = timestamp(data[field])
        if "first_observed_at" in data or "observed_at" in data:
            raise DataError("Local first observation is assigned by ingestion, not supplied by callers")
        sections = data.get("sections", [])
        claims = data.get("claims", [])
        if not isinstance(sections, list) or not isinstance(claims, list) or len(claims) > 5000:
            raise DataError("Evidence sections/claims must be lists; at most 5000 claims")
        parents = {}
        for section in sections:
            if not isinstance(section, dict) or not isinstance(section.get("id"), str) or not isinstance(section.get("text"), str):
                raise DataError("Parent sections require string id and text")
            if section["id"] in parents:
                raise DataError("Duplicate parent section ID")
            parents[section["id"]] = section
        seen = set()
        for claim in claims:
            if not isinstance(claim, dict):
                raise DataError("Claims must be objects")
            for field in ("id", "text", "facet"):
                if not isinstance(claim.get(field), str) or not claim[field].strip():
                    raise DataError("Claim requires nonempty " + field)
            if claim["id"] in seen:
                raise DataError("Duplicate claim ID")
            seen.add(claim["id"])
            if not re.fullmatch(r"[a-z][a-z0-9_-]*:[A-Za-z0-9_.-]+", str(claim.get("player_ref", ""))):
                raise DataError("Claims require exact provider-namespaced player references")
            if claim.get("kind", "observation") not in ("observation", "interpretation", "assumption", "forecast"):
                raise DataError("Unknown claim kind")
            claim.setdefault("kind", "observation")
            scope = claim.setdefault("scope", "shared")
            if scope not in ("shared", "decision") or scope == "decision" and not isinstance(claim.get("league_binding"), dict):
                raise DataError("Decision-specific claims require a league binding")
            if scope == "shared" and claim.get("league_binding"):
                raise DataError("Shared player evidence cannot carry league-specific effects")
            if claim.get("parent_id") is not None and claim["parent_id"] not in parents:
                raise DataError("Unknown parent section")
            for field in ("qualifiers", "counterevidence", "reversal_conditions"):
                if not isinstance(claim.get(field, []), list) or not all(isinstance(x, str) for x in claim.get(field, [])):
                    raise DataError(field + " must be a list of strings")
            if "span" in claim:
                span = claim["span"]
                parent = parents.get(claim.get("parent_id"), {})
                if (not isinstance(span, list) or len(span) != 2 or
                    not all(isinstance(x, int) and not isinstance(x, bool) for x in span) or
                    not 0 <= span[0] < span[1] <= len(parent.get("text", "")) or
                    parent["text"][span[0]:span[1]] != claim["text"]):
                    raise DataError("Exact extraction span does not match parent source text")
        data.setdefault("coverage", {"status": "partial", "gaps": ["Only supplied claims indexed; no exhaustive source/player coverage established"]})
        if len(encode(data).encode()) > 4_000_000:
            raise DataError("Evidence document exceeds 4 MB import bound")
        sha, now = digest(data), time.time()
        if preserved_record is not None:
            with self.store.db() as db:
                original = db.execute("SELECT kind,key,available_at,hash,body FROM versions WHERE id=?", (preserved_record["version_id"],)).fetchone()
            if original is None or original[0] != "dossier" or original[1] != preserved_record["key"] or original[3] != digest(json.loads(original[4])):
                raise DataError("Preserved dossier receipt does not match canonical Store version")
            if data.get("preserved_store_version") != {"version_id": preserved_record["version_id"], "key": original[1], "sha256": original[3]}:
                raise DataError("Indexed document must retain its verified Store version binding")
            now = original[2]
        with self.store.db() as db:
            # Acquire the write lock before stamping visibility: a blocked writer
            # must not appear knowable during a decision that preceded publication.
            db.execute("BEGIN IMMEDIATE")
            published_now = time.time()
            if preserved_record is None:
                now = published_now
            previous = db.execute("SELECT id,observed_at,hash FROM evidence_documents WHERE document_key=? ORDER BY observed_at DESC,id DESC LIMIT 1", (key,)).fetchone()
            exact = db.execute("SELECT id,observed_at,hash FROM evidence_documents WHERE document_key=? AND hash=? AND observed_at=? LIMIT 1", (key, sha, now)).fetchone() if preserved_record else None
            if exact:
                return {"document_id": exact[0], "first_observed_at": exact[1], "sha256": sha, "unchanged": True}
            if previous and previous[2] == sha:
                return {"document_id": previous[0], "first_observed_at": previous[1], "sha256": sha, "unchanged": True}
            cur = db.execute("INSERT INTO evidence_documents(document_key,observed_at,hash,body) VALUES(?,?,?,?)", (key, now, sha, encode(data)))
            ident = cur.lastrowid
            for claim in claims:
                cur = db.execute("INSERT INTO evidence_claims(document_id,claim_key,player_ref,facet,scope,binding,body) VALUES(?,?,?,?,?,?,?)", (ident, claim["id"], claim["player_ref"], claim["facet"], claim["scope"], encode(claim["league_binding"]) if claim.get("league_binding") else None, encode(claim)))
                db.execute("INSERT INTO evidence_fts(rowid,text,parent_text) VALUES(?,?,?)", (cur.lastrowid, claim["text"], parents.get(claim.get("parent_id"), {}).get("text", "")))
            # A same-transaction Store epoch marker lets decision receipt guards
            # detect index publication without requiring a second race-prone read.
            marker = {"document_id": ident, "document_key": key, "document_sha256": sha, "first_observed_at": now}
            db.execute("INSERT INTO versions(kind,key,available_at,hash,body,provenance) VALUES(?,?,?,?,?,?)",
                       ("evidence_index_epoch", key, published_now, digest(marker), encode(marker),
                        encode({"locally_available_at": published_now, "derived_from_preserved_record": preserved_record is not None})))
        return {"document_id": ident, "first_observed_at": now, "sha256": sha, "unchanged": False}

    def _card(self, row, detail):
        hit_id, document_id, observed, sha, body, claim_body = row
        document, claim = json.loads(body), json.loads(claim_body)
        if digest(document) != sha:
            raise DataError("Evidence document integrity failure")
        canonical = next((c for c in document["claims"] if c["id"] == claim["id"]), None)
        if canonical != claim:
            raise DataError("Indexed claim integrity failure")
        parent = next((s for s in document.get("sections", []) if s["id"] == claim.get("parent_id")), None)
        result = {"hit_id": hit_id, "document_id": document_id, "player_ref": claim["player_ref"],
                  "facet": claim["facet"], "claim": claim, "first_observed_at": observed,
                  "event_at": document.get("event_at"), "published_at": document.get("published_at"),
                  "source": document["source"], "coverage": document["coverage"],
                  "scope": claim["scope"], "truth_status": "not_independently_verified",
                  "content_role": "untrusted_evidence", "parent_handle": {"document_id": document_id, "section_id": claim.get("parent_id")},
                  "parent_locator": parent.get("locator") if parent else None,
                  "audit": {"sha256": sha, "source_locator": document["source"]["url"], "claim_locator": claim.get("locator"), "raw_sha256": document["source"].get("raw_sha256")}}
        # Never shorten a source assertion by slicing away its negation/qualifier.
        if detail == "decisive" and len(claim["text"]) > 2000:
            result["claim"] = {k: v for k, v in claim.items() if k != "text"}
            result["claim"]["text_status"] = "requires_expansion_whole_claim_exceeds_card_budget"
        if detail == "decisive" and len(encode(claim)) > 6000:
            result["claim"] = {k: claim[k] for k in ("id", "player_ref", "facet", "kind", "scope")}
            result["claim"].update(text_status="requires_expansion_whole_claim_exceeds_card_budget",
                                   qualifiers_count=len(claim.get("qualifiers", [])),
                                   counterevidence_count=len(claim.get("counterevidence", [])),
                                   content_status="Whole assertion and caveats must be expanded before relying")
        if detail == "decisive" and len(encode(result["source"])) > 2000:
            result["source"] = {k: document["source"][k] for k in ("url", "origin", "capture_policy")}
            result["source"]["additional_attribution_requires_expansion"] = True
        if detail in ("expanded", "raw"):
            result["parent"] = parent
        elif parent:
            if len(parent["text"]) <= 2000:
                result["parent"] = parent
            else:
                result["parent_context_required"] = True
                result["parent_text_status"] = "requires_expansion_whole_section_exceeds_card_budget"
        if detail == "raw":
            raw_sha = document["source"].get("raw_sha256")
            if raw_sha and hashlib.sha256((self.store.blobs / raw_sha).read_bytes()).hexdigest() != raw_sha:
                raise DataError("Original capture integrity failure")
            result["document"] = document
        return result

    def read(self, hit_id, *, as_of=None, detail="expanded"):
        if detail not in ("decisive", "expanded", "raw"):
            raise DataError("Unknown evidence detail")
        with self.store.read_view(as_of), self.store._read_db() as db:
            cutoff = self.store._read_cutoff(as_of)
            row = db.execute("SELECT c.id,d.id,d.observed_at,d.hash,d.body,c.body FROM evidence_claims c JOIN evidence_documents d ON d.id=c.document_id WHERE c.id=? AND d.observed_at<=?", (hit_id, cutoff)).fetchone()
            latest = db.execute("SELECT id FROM evidence_documents WHERE document_key=(SELECT document_key FROM evidence_documents WHERE id=?) AND observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT 1", (row[1], cutoff)).fetchone()[0] if row else None
        if row is None:
            raise DataError("Evidence hit was not observed at the requested cutoff")
        card = self._card(row, detail)
        card["revision_status"] = "latest_at_cutoff" if latest == row[1] else "superseded_at_cutoff"
        return card

    def search(self, query="", *, player_refs=None, facets=None, as_of=None, league_binding=None, limit=8, cursor=None, detail="decisive"):
        if detail not in ("decisive", "expanded", "raw"):
            raise DataError("Unknown evidence detail")
        if not isinstance(query, str) or len(query) > 1000:
            raise DataError("Evidence query must be a string of at most 1000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise DataError("Evidence page size must be between 1 and 50")
        if not isinstance(player_refs or [], (list, tuple)) or not isinstance(facets or [], (list, tuple)):
            raise DataError("Evidence player/facet filters must be lists")
        if not all(isinstance(x, str) for x in list(player_refs or []) + list(facets or [])):
            raise DataError("Evidence player/facet filters must be strings")
        refs, facets = sorted(set(player_refs or [])), sorted(set(facets or []))
        if len(refs) > 100 or len(facets) > 50:
            raise DataError("Evidence filters exceed bounded query limits")
        fingerprint = digest({"query": query, "players": refs, "facets": facets, "binding": league_binding, "detail": detail})
        cutoff, offset = self.store._read_cutoff(as_of), 0
        with self.store.read_view(cutoff), self.store._read_db() as db:
            upper = db.execute("SELECT COALESCE(MAX(id),0) FROM evidence_documents WHERE observed_at<=?", (cutoff,)).fetchone()[0]
            if cursor:
                try:
                    token = json.loads(base64.urlsafe_b64decode(cursor))
                    if token["fingerprint"] != fingerprint or as_of is not None and timestamp(as_of) != token["cutoff"]:
                        raise ValueError()
                    cutoff, offset, upper = token["cutoff"], token["offset"], token["upper"]
                    if not isinstance(offset, int) or offset < 0 or not isinstance(upper, int) or upper < 0:
                        raise ValueError()
                    cutoff = self.store._read_cutoff(cutoff)
                except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError) as exc:
                    raise DataError("Invalid cursor or changed evidence query") from exc
            where = ["d.observed_at<=?", "d.id<=?", "NOT EXISTS (SELECT 1 FROM evidence_documents newer WHERE newer.document_key=d.document_key AND newer.observed_at<=? AND newer.id<=? AND (newer.observed_at>d.observed_at OR (newer.observed_at=d.observed_at AND newer.id>d.id)))"]
            params = [cutoff, upper, cutoff, upper]
            join = ""
            # Escape each term: imported/query text cannot inject FTS operators.
            words = re.findall(r"\w+", query, re.UNICODE)
            if query.strip() and not words:
                raise DataError("Evidence text query needs at least one searchable term")
            if words:
                join = " JOIN evidence_fts ON evidence_fts.rowid=c.id"
                where.append("evidence_fts MATCH ?")
                params.append(" AND ".join('"' + word.replace('"', '""') + '"' for word in words))
            for column, values in (("c.player_ref", refs), ("c.facet", facets)):
                if values:
                    where.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                    params.extend(values)
            if league_binding is None:
                where.append("c.scope='shared'")
            else:
                where.append("(c.scope='shared' OR c.binding=?)")
                params.append(encode(league_binding))
            # Revision-pinned ID ordering also avoids BM25 changes when new rows arrive.
            sql = "SELECT c.id,d.id,d.observed_at,d.hash,d.body,c.body FROM evidence_claims c JOIN evidence_documents d ON d.id=c.document_id" + join + " WHERE " + " AND ".join(where) + " ORDER BY d.id DESC,c.id ASC LIMIT ? OFFSET ?"
            rows = db.execute(sql, params + [limit + 1, offset]).fetchall()
            total = db.execute("SELECT COUNT(*) FROM evidence_claims c JOIN evidence_documents d ON d.id=c.document_id" + join + " WHERE " + " AND ".join(where), params).fetchone()[0]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = base64.urlsafe_b64encode(encode({"fingerprint": fingerprint, "cutoff": cutoff, "upper": upper, "offset": offset + limit}).encode()).decode()
        return {"items": [self._card(row, detail) for row in rows[:limit]], "as_of": cutoff,
                "total_matching": total, "next_cursor": next_cursor, "candidate_player_refs": refs,
                "coverage": {"status": "partial", "indexed_documents_through": upper,
                             "meaning": "Latest locally observed revisions matching supplied filters; no completeness of NFL evidence implied"},
                "exclusions": ["Later local observations", "Superseded revisions", "Other league-specific effects"],
                "ordering": "newest_document_revision_then_claim_id"}

    def health(self):
        """Check structural index consistency without certifying source truth."""
        with self.store.db() as db:
            db.execute("INSERT INTO evidence_fts(evidence_fts) VALUES('integrity-check')")
            counts = db.execute("SELECT (SELECT COUNT(*) FROM evidence_claims), (SELECT COUNT(*) FROM evidence_fts)").fetchone()
            missing = db.execute("SELECT COUNT(*) FROM evidence_claims c LEFT JOIN evidence_fts f ON f.rowid=c.id WHERE f.rowid IS NULL").fetchone()[0]
        if counts[0] != counts[1] or missing:
            raise DataError("Derived evidence index is incomplete; rebuild from canonical documents")
        return {"status": "structurally_consistent", "claims": counts[0], "truth_verified": False}

    def rebuild_index(self):
        """Reconstruct derived FTS rows atomically, keeping historical IDs/dates."""
        with self.store.db() as db:
            rows = db.execute("SELECT c.id,d.id,d.observed_at,d.hash,d.body,c.body FROM evidence_claims c JOIN evidence_documents d ON d.id=c.document_id ORDER BY c.id").fetchall()
            db.execute("DELETE FROM evidence_fts")
            for row in rows:
                card = self._card(row, "expanded")
                db.execute("INSERT INTO evidence_fts(rowid,text,parent_text) VALUES(?,?,?)", (row[0], card["claim"]["text"], (card.get("parent") or {}).get("text", "")))
        return {"status": "rebuilt", "claims": len(rows), "historical_dates_changed": False}

    def manifest(self, player_refs, *, as_of=None, league_binding=None):
        """Latest versions of historically relevant keys, including tombstones.

        This read never migrates or writes. A document that removes every claim
        must still invalidate a prior decision that consumed its older claims.
        """
        if not player_refs:
            return []
        cutoff = self.store._read_cutoff(as_of)
        with self.store.read_view(cutoff), self.store._read_db() as db:
            params = list(player_refs) + [cutoff]
            clause = "c.scope='shared'"
            if league_binding is not None:
                clause = "(c.scope='shared' OR c.binding=?)"
                params.append(encode(league_binding))
            params.extend([cutoff, cutoff])
            rows = db.execute("WITH relevant AS (SELECT DISTINCT old.document_key FROM evidence_documents old JOIN evidence_claims c ON c.document_id=old.id WHERE c.player_ref IN (" + ",".join("?" for _ in player_refs) + ") AND old.observed_at<=? AND " + clause + ") SELECT d.id,d.document_key,d.observed_at,d.hash FROM evidence_documents d JOIN relevant r ON r.document_key=d.document_key WHERE d.observed_at<=? AND NOT EXISTS (SELECT 1 FROM evidence_documents n WHERE n.document_key=d.document_key AND n.observed_at<=? AND (n.observed_at>d.observed_at OR (n.observed_at=d.observed_at AND n.id>d.id))) ORDER BY d.document_key,d.id", params).fetchall()
        return [{"kind": "evidence_document", "key": row[1], "version_id": row[0], "available_at": row[2], "sha256": row[3]} for row in rows]
