"""Open Data Contract Standard (ODCS) v3.2.0: a referential published as a data contract.

ODCS (Bitol, LF AI & Data) describes what a dataset **is** — schema, quality, ownership, service levels — so
that a producer and its consumers agree on it in a form a tool can read. A referential definition already
holds all of that, next to something the standard deliberately does not model: how the data is reached,
decoded and published.

So this module only goes one way. There is no reader turning a contract back into a definition, and that is
not an omission: ODCS has no equivalent of a source (urls, extraction, retries), of a transformation, of
chained deltas, of a merge or of a unification, so a definition rebuilt from a contract would be a definition
that cannot run.

Only four fields are mandatory in the standard (`apiVersion`, `kind`, `id`, `version`) and everything else is
optional. What is absent from the document this module produces is absent because the project has nothing
truthful to put in it, never to pad the output.

Two parts come from the published version rather than from the declaration, because they describe what the
data actually turned out to be:

- `meta["columns"]` — the columns of the published Parquet file, which a definition usually does not declare
  (`columns` is for internal referentials only).
- `meta["profile"]` — the column profile, which is what lets the export state `required` and `examples` from
  measurements instead of from intentions.

The profile holds real values (its `min` and `max` are two rows of the referential), and **it is readable
here even for a confidential referential**: ingest.py stores it encrypted (`profile_enc`), but service.py
decrypts it into the in-memory metadata so the interface can show it (`_in_memory`). So the guard below is
explicit and must stay that way: no `examples` for a confidential referential, and none for a personal data
column, whatever the profile happens to contain. A contract is written to be handed to people outside the
instance, which is not the same exposure as an authenticated read of the catalogue.

Column *names* are exported in every case: they are not secret, every reader of the referential sees them,
and a contract without them would describe nothing.
"""

from __future__ import annotations

from typing import Any

from .config import ReferentialConfig
from .quality import QualityRule, effective_rules
from .sqlbuild import qi

APIVERSION = "v3.2.0"
KIND = "DataContract"
# Registered media type of the standard, which is what a consumer content-negotiates on
MEDIA_TYPE = f"application/odcs+yaml;version={APIVERSION.lstrip('v')}"

# DuckDB physical types -> the closed `logicalType` enum of ODCS. The physical type is exported beside it, so
# nothing is lost by this narrowing; only the prefix is matched, since the physical type carries a width
# (DECIMAL(18,2), VARCHAR(10)) that the logical type has no room for.
_LOGICAL: tuple[tuple[str, str], ...] = (
    ("BOOLEAN", "boolean"),
    ("TIMESTAMP", "timestamp"),
    ("DATE", "date"),
    ("TIME", "time"),
    ("VARCHAR", "string"),
    ("UUID", "string"),
    ("BLOB", "string"),
    ("JSON", "object"),
    ("STRUCT", "object"),
    ("MAP", "map"),
    ("UNION", "object"),
    ("LIST", "array"),
    ("ARRAY", "array"),
    ("DECIMAL", "number"),
    ("DOUBLE", "number"),
    ("FLOAT", "number"),
    ("REAL", "number"),
    ("HUGEINT", "integer"),
    ("BIGINT", "integer"),
    ("INTEGER", "integer"),
    ("SMALLINT", "integer"),
    ("TINYINT", "integer"),
    ("UBIGINT", "integer"),
    ("UINTEGER", "integer"),
    ("USMALLINT", "integer"),
    ("UTINYINT", "integer"),
)

# The `dimension` enum of ODCS, which is closed: our checks say *how* they verify, the dimension says *what*
# they are about, and a consumer aggregates its quality reporting on it.
_DIMENSION: dict[str, str] = {
    "not_null": "completeness",
    "unique": "uniqueness",
    "format": "conformity",
    "pattern": "conformity",
    "values": "conformity",
    "length": "conformity",
    "range": "accuracy",
    "expression": "consistency",
}


def logical_type(physical: str | None) -> str:
    """The ODCS logical type for a DuckDB type. `string` for anything unrecognised: the physical type travels
    beside it, so a consumer that knows DuckDB keeps the full information."""
    up = (physical or "").strip().upper()
    for prefix, logical in _LOGICAL:
        if up.startswith(prefix):
            return logical
    return "string"


def _clean(obj: Any) -> Any:
    """Drop the empty values, recursively. ODCS makes everything optional, and a contract that states
    `description: ""` or `tags: []` claims something it does not know; leaving the key out says nothing, which
    is the truth. `False` and `0` are answers, so they stay."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = _clean(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        kept = [_clean(v) for v in obj]
        return [v for v in kept if v not in (None, "", [], {})]
    return obj


# --------------------------------------------------------------------------- quality

def _failing_sql(rule: QualityRule) -> str | None:
    """The SQL condition that is true for the rows failing `rule`.

    quality.py already compiles every rule to exactly this, to check it in one scan; re-deriving it here would
    mean two definitions of the same rule, free to drift apart. So the contract exports what is actually run.
    """
    from .quality import _failing

    return _failing(rule)


def _check(rule: QualityRule, table: str, automatic: bool) -> dict[str, Any]:
    """One rule as an ODCS data quality check.

    `not_null` and `unique` map onto the predefined metrics of the standard (`nullValues`, `duplicateValues`),
    which is what a consumer can run without understanding our SQL — but only at a threshold of 100 %, since
    the metrics count rows while a threshold is a proportion. Everything else, and every partial threshold,
    becomes a `sql` check: the standard's metric library has nothing for a regular expression, a range or an
    SQL condition, and inventing an approximation of them would be worse than exporting the query.
    """
    base: dict[str, Any] = {
        "name": rule.label,
        "description": f"Checked by RefExposer on every new version{' (implied by the key)' if automatic else ''}",
        "dimension": _DIMENSION.get(rule.check),
        "severity": rule.severity,
    }
    strict = rule.threshold >= 100
    if rule.check == "not_null" and strict:
        return {**base, "type": "library", "metric": "nullValues", "unit": "rows", "mustBe": 0,
                **({"arguments": {"column": rule.column}} if rule.column else {})}
    if rule.check == "unique" and strict:
        return {**base, "type": "library", "metric": "duplicateValues", "unit": "rows", "mustBe": 0,
                **({"arguments": {"column": rule.column}} if rule.column else {})}

    failing = _failing_sql(rule)
    if failing is None:  # `unique` under a partial threshold: counted by grouping, not row by row
        col = qi(rule.column) if rule.column else ""
        query = (f"SELECT 100.0 * (count(*) - count(DISTINCT {col})) / nullif(count(*), 0) "
                 f"FROM {qi(table)}")
    else:
        query = (f"SELECT 100.0 * count(*) FILTER (WHERE {failing}) / nullif(count(*), 0) "
                 f"FROM {qi(table)}")
    return {**base, "type": "sql", "query": query, "unit": "percent",
            "mustBeLessOrEqualTo": round(100.0 - rule.threshold, 6)}


def quality_checks(ref: ReferentialConfig) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """(checks on the dataset, checks per column) for a referential.

    A rule on a column is attached to that column, which is where a consumer looks for it; a rule with no
    column (an SQL condition over the row) belongs to the object, and so does the minimum number of rows.
    """
    on_object: list[dict[str, Any]] = []
    on_column: dict[str, list[dict[str, Any]]] = {}
    for rule, automatic in effective_rules(ref.quality, ref.key):
        check = _check(rule, ref.table, automatic)
        if rule.column:
            on_column.setdefault(rule.column, []).append(check)
        else:
            on_object.append(check)
    minimum = ref.validation.min_rows
    if minimum:
        # Kept even at the default of one row: "an empty download never replaces the published data" is a
        # real guarantee of the ingestion, and one a consumer has no other way of learning.
        on_object.append({
            "name": f"at least {minimum:,} row{'s' if minimum > 1 else ''}".replace(",", " "),
            "description": "A version with fewer rows is rejected and the published one is kept",
            "dimension": "completeness", "severity": "error",
            "type": "library", "metric": "rowCount", "unit": "rows",
            "mustBeGreaterOrEqualTo": minimum,
        })
    if ref.validation.unique_key and ref.key:
        # Declared, so stronger than what the profile measures: a version where the key repeats is refused
        on_column.setdefault(ref.key, []).append({
            "name": f"{ref.key} is unique",
            "description": "A version where the key is not unique is rejected",
            "dimension": "uniqueness", "severity": "error",
            "type": "library", "metric": "duplicateValues", "unit": "rows", "mustBe": 0,
            "arguments": {"column": ref.key},
        })
    if ref.validation.max_drop_pct is not None:
        # Not a metric of the standard: it compares a version with the previous one, which no single query
        # can do. Stated as text so the promise is at least readable.
        on_object.append({
            "name": f"no more than {ref.validation.max_drop_pct} % of the rows lost",
            "description": "Compared with the published version; a larger drop is rejected",
            "dimension": "completeness", "severity": "error", "type": "text",
        })
    return on_object, on_column


# --------------------------------------------------------------------------- schema

def _property(name: str, physical: str | None, profile: dict[str, Any] | None, ref: ReferentialConfig,
              checks: list[dict[str, Any]], declared: dict[str, Any] | None,
              key_unique: bool | None) -> dict[str, Any]:
    pii = [c.lower() for c in (ref.pii.columns if ref.pii else [])]
    keys = ref.primary_key_columns or ([ref.key] if ref.key else [])
    personal = name.lower() in pii
    prop: dict[str, Any] = {
        "name": name,
        "logicalType": logical_type(physical),
        "physicalType": physical,
        "description": (declared or {}).get("description") or "",
    }
    if name in keys:
        prop["primaryKey"] = True
        prop["primaryKeyPosition"] = keys.index(name) + 1
        # Only the key is measured exactly (ingest counts its distinct values); the profile knows no more
        # than an *approximate* cardinality for the other columns, and a contract must not promise
        # uniqueness on an approximation.
        if key_unique is not None and name == (keys[0] if keys else None):
            prop["unique"] = bool(key_unique)
    if any(c.get("metric") == "duplicateValues" for c in checks):
        prop["unique"] = True  # a uniqueness rule is checked at every update, so it is a promise
    if declared and declared.get("required") is not None:
        prop["required"] = bool(declared["required"])
    if profile:
        nulls = profile.get("null_percentage")
        if nulls is not None:
            # Measured on the published version, so `required` here means "has always been filled", which is
            # what a consumer can actually rely on
            prop.setdefault("required", float(nulls) == 0.0)
        if not personal and not ref.confidential:
            # The two ends of the column, which the profile already holds. These are real rows of the
            # referential, so they are withheld from a personal data column and from a confidential one --
            # explicitly, because the profile is decrypted in memory and would otherwise be exported.
            prop["examples"] = [v for v in (profile.get("min"), profile.get("max")) if v is not None]
    if personal:
        prop["classification"] = "personal"
        prop["criticalDataElement"] = True
        if ref.pii and ref.pii.note:
            prop["description"] = (prop["description"] + " — " if prop["description"] else "") + ref.pii.note
    if ref.confidential:
        prop["classification"] = "confidential"
    semantic = ref.entities.get(name)
    if semantic:
        # What the values *are* (a CVE, a CPE, an IP), which is how the catalogue relates columns across
        # referentials (entities.py). ODCS has a field for exactly that.
        prop["semanticType"] = semantic
    if checks:
        prop["quality"] = checks
    return _clean(prop)


def _schema(ref: ReferentialConfig, meta: dict[str, Any]) -> list[dict[str, Any]]:
    columns = meta.get("columns") or []
    profiles = {p.get("name"): p for p in (meta.get("profile") or []) if p.get("name")}
    declared = {c.name: c.model_dump(mode="json") for c in ref.columns}
    on_object, on_column = quality_checks(ref)

    properties = []
    for col in columns:
        name = col if isinstance(col, str) else col.get("name")
        if not name:
            continue
        physical = None if isinstance(col, str) else col.get("type")
        if physical is None:
            physical = (profiles.get(name) or {}).get("type")
        properties.append(_property(name, physical, profiles.get(name), ref,
                                    on_column.get(name, []), declared.get(name),
                                    meta.get("key_unique")))

    obj: dict[str, Any] = {
        "name": ref.id,
        "physicalName": ref.table,
        "logicalType": "object",
        "physicalType": "parquet",
        "description": ref.description,
        "businessName": ref.name,
        "tags": list(ref.tags),
        "properties": properties,
        "quality": on_object,
    }
    rows = meta.get("row_count")
    if rows is not None:
        obj["dataGranularityDescription"] = f"One row per {ref.key or 'record'} ({rows:,} rows published)".replace(",", " ")
    return [_clean(obj)]


# --------------------------------------------------------------------------- the document

def _servers(ref: ReferentialConfig, base_url: str) -> list[dict[str, Any]]:
    """Where a consumer reads the data. The REST API is always there; a referential whose home is a bucket
    also names it, without the credentials, because that is how a data platform reads it without us."""
    servers: list[dict[str, Any]] = [{
        "server": "refexposer-api",
        "type": "api",
        "location": f"{base_url.rstrip('/')}/api/referentials/{ref.id}",
        "description": "REST API: /rows, /lookup, /export and /download serve this referential",
    }]
    if ref.storage.location == "remote":
        # The bucket of Settings > Storage, which is the home of the data when it is remote. The endpoint and
        # the bucket are a deployment setting, not part of the definition, so only the shape is stated.
        servers.append({
            "server": "refexposer-storage",
            "type": "s3",
            "format": "parquet",
            "description": "Published Parquet file, in the bucket configured for this instance",
        })
    return [_clean(s) for s in servers]


def _team(ref: ReferentialConfig) -> list[dict[str, Any]]:
    out = []
    if ref.owner:
        out.append({"username": ref.owner, "role": "owner",
                    "description": "Accountable for this referential"})
    if ref.owner_team:
        out.append({"username": ref.owner_team, "role": "owner_team",
                    "description": "Team the referential belongs to"})
    return [_clean(m) for m in out]


def _roles(ref: ReferentialConfig) -> list[dict[str, Any]]:
    """The rights that exist on a referential, not who holds them: a contract says how access is obtained,
    and naming the accounts would publish the access list of the instance."""
    roles: list[dict[str, Any]] = [
        {"role": "read", "access": "read",
         "description": "Read the rows, the exports and the generated downloads"
                        + (" (public: no account needed)" if ref.visibility == "public" else "")},
        {"role": "manage", "access": "write",
         "description": "Change the definition and start an update"},
    ]
    if ref.pii and ref.pii.columns:
        roles.append({
            "role": "pii-reveal", "access": "read-unmasked",
            "description": "See the personal data columns in clear, on explicit and audited request",
            "firstLevelApprovers": ", ".join(ref.pii.reveal_groups) or None,
        })
    return [_clean(r) for r in roles]


def _sla(ref: ReferentialConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if ref.schedule:
        out.append({"property": "frequency", "value": ref.schedule, "unit": "cron",
                    "driver": "operational", "element": ref.id,
                    "description": "Schedule the referential is updated on"})
    if ref.max_age:
        out.append({"property": "freshness", "value": ref.max_age, "driver": "operational", "element": ref.id,
                    "description": "Past this age the published data is reported as stale"})
    st = ref.storage
    if st.time_travel:
        out.append({"property": "retention", "value": st.max_snapshots, "unit": "versions",
                    "driver": "operational", "element": ref.id,
                    "description": "Past versions kept and queryable (time travel)"})
    return [_clean(s) for s in out]


def _authoritative(ref: ReferentialConfig) -> list[dict[str, Any]]:
    out = []
    if ref.homepage:
        out.append({"url": ref.homepage, "type": "businessDefinition",
                    "description": "Home page of the source"})
    for url in (ref.source.urls or [])[:10]:
        out.append({"url": url, "type": "implementation", "description": "Source the data is fetched from"})
    return [_clean(a) for a in out]


def _custom(ref: ReferentialConfig, meta: dict[str, Any]) -> list[dict[str, Any]]:
    """What the standard has no field for, and that a consumer still needs to understand the dataset.

    `customProperties` is the only honest place for it: inventing a top-level field would produce a document
    no other ODCS tool can read.
    """
    out = [
        {"property": "refexposer.sourceType", "value": ref.source.type,
         "description": "How RefExposer obtains the data (no ODCS equivalent)"},
        {"property": "refexposer.format", "value": ref.format},
        {"property": "refexposer.visibility", "value": ref.visibility},
        {"property": "refexposer.confidential", "value": ref.confidential},
    ]
    if ref.license:
        out.append({"property": "refexposer.license", "value": ref.license})
    if meta.get("version") is not None:
        out.append({"property": "refexposer.dataVersion", "value": meta.get("version"),
                    "description": "Version of the data, which the contract version does not track"})
    if meta.get("data_updated_at"):
        out.append({"property": "refexposer.dataUpdatedAt", "value": meta.get("data_updated_at")})
    if ref.transform:
        out.append({"property": "refexposer.transform", "value": ref.transform,
                    "description": "SQL applied to the source before publication"})
    if ref.combine:
        out.append({"property": "refexposer.combine", "value": ref.combine.mode,
                    "description": "Unified referential: built from other referentials"})
    if ref.federation:
        out.append({"property": "refexposer.federatedFrom", "value": ref.federation.peer,
                    "description": "Replica of a referential of another instance"})
    if ref.merge and ref.merge.enabled:
        out.append({"property": "refexposer.merge", "value": True,
                    "description": "Each update is merged into the published version instead of replacing it"})
    return [_clean(c) for c in out]


def contract(ref: ReferentialConfig, meta: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    """The referential as an ODCS v3.2.0 document.

    `meta` is the metadata of the published version (service.metas): an empty dict produces a contract
    describing what is promised, with no measured schema — which is what a referential that never ran is.
    """
    cfg = ref.contract
    doc: dict[str, Any] = {
        "apiVersion": APIVERSION,
        "kind": KIND,
        "id": ref.id,
        "version": cfg.version,
        "name": ref.name,
        "status": cfg.status,
        "domain": ref.category,
        "tags": list(ref.tags),
        "description": {
            # `purpose` falls back on the description of the referential: a contract with no purpose at all is
            # poorer than one stating the only sentence we have.
            "purpose": cfg.purpose or ref.description,
            "usage": cfg.usage,
            "limitations": cfg.limitations,
        },
        "servers": _servers(ref, base_url),
        "schema": _schema(ref, meta),
        "team": _team(ref),
        "roles": _roles(ref),
        "slaProperties": _sla(ref),
        "support": [c.model_dump(mode="json") for c in cfg.support],
        "authoritativeDefinitions": _authoritative(ref),
        "customProperties": _custom(ref, meta),
    }
    return _clean(doc)
