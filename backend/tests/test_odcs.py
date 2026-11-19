"""The data contract of a referential: Open Data Contract Standard (ODCS) v3.2.0.

The export is checked against the **vendored schema of the standard** (`odcs-json-schema-3.2.0.json`, copied
from bitol-io/open-data-contract-standard), not against a copy of our own expectations: the point of a
standard is that someone else's tool reads the document, so the test fails when the standard moves and the
schema is refreshed, which is exactly when we want to hear about it.

`jsonschema` is not a dependency of the project, and adding one to the pinned lock for a test would be a poor
trade. So `conform()` below reads the vendored schema and enforces the parts a hand-written projection can
actually get wrong: the mandatory fields, the closed enumerations, and the required keys of every object we
emit. It does not implement JSON Schema.
"""
import json
import os

import pytest
import yaml

from conftest import USER_PASSWORD, create_user, new_client, refresh

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "odcs-json-schema-3.2.0.json")

PEOPLE = """id,email,full_name,dept,salary
1,alice@example.com,Alice Martin,IT,42000
2,bob@example.com,Bob Durand,HR,39000
3,carol@example.com,Carol Petit,IT,45000
"""

DEFINITION = {
    "referentials": [{
        "id": "people",
        "name": "Staff directory",
        "category": "HR",
        "tags": ["hr", "internal"],
        "homepage": "https://intranet.example.com/staff",
        "license": "CC-BY-4.0",
        "owner": "dpo@example.com",
        "owner_team": "Security",
        "visibility": "internal",
        "source": {"type": "local", "path": "raw/*.csv"},
        "format": "csv",
        "key": "id",
        "primary_key_columns": ["id"],
        "schedule": "0 4 * * *",
        "max_age": "2d",
        "entities": {"email": "email"},
        "validation": {"min_rows": 2, "max_drop_pct": 50},
        "pii": {"columns": ["email", "full_name"], "strategy": "hash", "reveal_groups": ["dpo"],
                "note": "GDPR: staff directory, 3 years"},
        "quality": {"rules": [
            {"column": "email", "check": "format", "format": "email", "severity": "error"},
            {"column": "dept", "check": "values", "values": ["IT", "HR"], "threshold": 90},
            {"column": "salary", "check": "range", "min": 0, "max": 1000000},
            {"check": "expression", "expression": "salary >= 0", "name": "no negative salary"},
        ]},
        "storage": {"time_travel": True, "max_snapshots": 4},
        "contract": {
            "version": "2.1.0",
            "status": "active",
            "purpose": "Who works here, for the access reviews",
            "usage": "Join on id; the e-mail columns are masked",
            "limitations": "Contractors are not in it",
            "support": [{"channel": "#data-platform", "tool": "slack", "scope": "interactive",
                         "url": "https://example.slack.com/archives/C01"}],
        },
    }]
}


def _write(env, csv: str = PEOPLE, definition: dict | None = None) -> None:
    config = env.data_dir.parent / "config"
    raw = env.data_dir / "people" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "p.csv").write_text(csv, encoding="utf-8")
    (config / "people.yml").write_text(yaml.safe_dump(definition or DEFINITION, sort_keys=False),
                                       encoding="utf-8")
    assert env.post("/api/system/reload").status_code == 200


@pytest.fixture()
def people(env):
    _write(env)
    refresh(env, "people")
    return "people"


def get(env, ref="people", **params):
    r = env.get(f"/api/referentials/{ref}/contract", params=params)
    assert r.status_code == 200, r.text
    return r


def doc(env, ref="people"):
    return get(env, ref, format="json").json()


# --------------------------------------------------------------------------- conformance

def conform(document: dict) -> list[str]:
    """What the vendored schema of the standard says about this document, as a list of problems.

    Not a JSON Schema implementation: the mandatory fields, the closed enumerations and the `required` keys of
    the objects we emit, resolved through `$ref` and `allOf`. Enough to catch a renamed field, a value outside
    an enumeration, or a required key we stopped emitting.
    """
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    defs = schema["$defs"]
    problems: list[str] = []

    def resolve(node: dict) -> dict:
        """A node with its `$ref` and its `allOf` merged in, so `required`/`properties`/`enum` are all here."""
        out: dict = {}
        stack = [node]
        while stack:
            cur = stack.pop()
            if "$ref" in cur:
                stack.append(defs[cur["$ref"].split("/")[-1]])
                continue
            for part in cur.get("allOf", []):
                stack.append(part)
            for key in ("properties", "required", "enum", "type"):
                if key in cur:
                    if key == "properties":
                        out.setdefault("properties", {}).update(cur["properties"])
                    elif key == "required":
                        out.setdefault("required", []).extend(cur["required"])
                    else:
                        out.setdefault(key, cur[key])
        return out

    def walk(value, node: dict, path: str) -> None:
        node = resolve(node)
        if isinstance(value, dict):
            for key in node.get("required", []):
                if key not in value:
                    problems.append(f"{path}: missing required '{key}'")
            for key, sub in value.items():
                spec = (node.get("properties") or {}).get(key)
                if spec is None:
                    continue  # the standard allows what it does not describe; customProperties live there
                walk(sub, spec, f"{path}.{key}")
            return
        if isinstance(value, list):
            items = node.get("items")
            if items:
                for i, item in enumerate(value):
                    walk(item, items, f"{path}[{i}]")
            return
        enum = node.get("enum")
        if enum is not None and value not in enum:
            problems.append(f"{path}: {value!r} is not one of {enum}")

    walk(document, schema, "$")
    return problems


def test_the_contract_conforms_to_the_standard(env, people):
    """The whole point: a document another ODCS tool can read."""
    contract = doc(env)
    assert conform(contract) == []
    assert contract["apiVersion"] == "v3.2.0" and contract["kind"] == "DataContract"
    assert contract["id"] == "people" and contract["version"] == "2.1.0"


def test_a_referential_that_never_ran_still_has_a_contract(env):
    """A contract states what is promised; it does not need a published version to exist."""
    _write(env)
    contract = doc(env)
    assert conform(contract) == []
    assert contract["status"] == "active"
    # No measured schema yet: the object is there, its columns are not invented
    assert contract["schema"][0]["name"] == "people"
    assert not contract["schema"][0].get("properties")


def test_the_yaml_form_carries_the_media_type_of_the_standard(env, people):
    r = get(env)
    assert r.headers["content-type"].startswith("application/odcs+yaml;version=3.2.0")
    assert 'filename="people-contract.yaml"' in r.headers["content-disposition"]
    parsed = yaml.safe_load(r.text)
    assert parsed == doc(env)  # the two formats say the same thing


# --------------------------------------------------------------------------- what it says

def test_the_schema_comes_from_the_published_version(env, people):
    """`columns` is not declared here: a contract that described the intention would describe nothing."""
    obj = doc(env)["schema"][0]
    by_name = {p["name"]: p for p in obj["properties"]}
    assert set(by_name) == {"id", "email", "full_name", "dept", "salary"}
    assert by_name["id"]["logicalType"] == "integer" and by_name["id"]["physicalType"] == "BIGINT"
    assert by_name["email"]["logicalType"] == "string"
    assert by_name["id"]["primaryKey"] is True and by_name["id"]["primaryKeyPosition"] == 1
    assert by_name["id"]["unique"] is True  # measured exactly for the key
    assert "unique" not in by_name["dept"]  # an approximate cardinality promises nothing
    assert by_name["email"]["semanticType"] == "email"
    assert obj["physicalName"] == "people" and obj["businessName"] == "Staff directory"


def test_the_personal_data_columns_are_flagged_and_never_sampled(env, people):
    by_name = {p["name"]: p for p in doc(env)["schema"][0]["properties"]}
    for column in ("email", "full_name"):
        assert by_name[column]["classification"] == "personal"
        assert by_name[column]["criticalDataElement"] is True
        assert "examples" not in by_name[column]
        assert "GDPR" in by_name[column]["description"]
    assert by_name["dept"]["examples"]  # a column that is not personal data keeps its two ends
    assert "alice@example.com" not in json.dumps(doc(env))
    assert "Alice Martin" not in json.dumps(doc(env))


def test_a_confidential_referential_publishes_no_value(env):
    """No example from the data, although the profile *is* readable here.

    The column profile of a confidential referential is stored encrypted but decrypted into the in-memory
    metadata so the interface can show it (service._in_memory). The guard in odcs.py is therefore explicit,
    and this test is what keeps it: a contract is handed to people outside the instance.
    """
    definition = {"referentials": [{**DEFINITION["referentials"][0], "confidential": True, "pii": None}]}
    _write(env, definition=definition)
    refresh(env, "people")
    assert env.get("/api/referentials/people").json()["profile"], "the profile is readable: the guard is not free"

    contract = doc(env)
    assert conform(contract) == []
    properties = contract["schema"][0]["properties"]
    assert {p["name"] for p in properties} == {"id", "email", "full_name", "dept", "salary"}  # names are not secret
    assert all("examples" not in p for p in properties)
    assert all(p["classification"] == "confidential" for p in properties)
    for value in ("alice@example.com", "Alice Martin", "Carol Petit", "42000", "39000", "45000"):
        assert value not in json.dumps(contract)
    # The values *declared* in a rule are configuration, not data: every reader of the referential already
    # sees them in the catalogue, and a quality check stripped of them would promise nothing.
    assert "'IT', 'HR'" in json.dumps(contract)


def test_the_quality_rules_become_odcs_checks(env, people):
    contract = doc(env)
    obj = contract["schema"][0]
    by_name = {p["name"]: p for p in obj["properties"]}

    # The key is checked for completeness at every update, even without a rule: a predefined metric
    key_checks = by_name["id"]["quality"]
    assert any(c.get("metric") == "nullValues" and c["mustBe"] == 0 and c["unit"] == "rows" for c in key_checks)
    assert all(c["dimension"] == "completeness" for c in key_checks)

    # A regular expression has no metric in the standard: exported as the SQL that is actually run
    email = by_name["email"]["quality"][0]
    assert email["type"] == "sql" and email["dimension"] == "conformity" and email["severity"] == "error"
    assert "regexp_full_match" in email["query"] and email["mustBeLessOrEqualTo"] == 0.0

    # A partial threshold is a proportion, so it cannot be a row-counting metric
    dept = by_name["dept"]["quality"][0]
    assert dept["type"] == "sql" and dept["unit"] == "percent" and dept["mustBeLessOrEqualTo"] == 10.0

    # A rule over the row, and the minimum number of rows, belong to the object
    object_checks = {c["name"]: c for c in obj["quality"]}
    assert object_checks["no negative salary"]["type"] == "sql"
    rows = next(c for c in obj["quality"] if c.get("metric") == "rowCount")
    assert rows["mustBeGreaterOrEqualTo"] == 2 and rows["severity"] == "error"
    assert rows["name"] == "at least 2 rows"
    # Comparing two versions is no single query: stated as text rather than faked as a metric
    assert any(c["type"] == "text" and "lost" in c["name"] for c in obj["quality"])


def test_a_declared_unique_key_is_a_promise_not_a_measurement(env):
    """`validation.unique_key` refuses a version where the key repeats: stronger than the profile."""
    definition = {"referentials": [{**DEFINITION["referentials"][0],
                                    "validation": {"min_rows": 1, "unique_key": True}}]}
    _write(env, definition=definition)
    refresh(env, "people")
    contract = doc(env)
    assert conform(contract) == []
    key = next(p for p in contract["schema"][0]["properties"] if p["name"] == "id")
    check = next(c for c in key["quality"] if c.get("metric") == "duplicateValues")
    assert check["mustBe"] == 0 and check["severity"] == "error"
    assert key["unique"] is True
    # The default minimum is still a guarantee worth stating, and it reads properly in the singular
    assert any(c["name"] == "at least 1 row" for c in contract["schema"][0]["quality"])


def test_the_governance_of_the_referential_is_in_the_contract(env, people):
    contract = doc(env)
    assert contract["domain"] == "HR" and contract["tags"] == ["hr", "internal"]
    assert contract["description"]["purpose"].startswith("Who works here")
    assert contract["description"]["limitations"] == "Contractors are not in it"
    assert [m["username"] for m in contract["team"]] == ["dpo@example.com", "Security"]
    assert contract["support"][0]["channel"] == "#data-platform"

    roles = {r["role"]: r for r in contract["roles"]}
    assert roles["read"]["access"] == "read" and roles["manage"]["access"] == "write"
    assert roles["pii-reveal"]["firstLevelApprovers"] == "dpo"  # the group, not the accounts in it

    sla = {s["property"]: s for s in contract["slaProperties"]}
    assert sla["frequency"]["value"] == "0 4 * * *" and sla["frequency"]["unit"] == "cron"
    assert sla["freshness"]["value"] == "2d"
    assert sla["retention"]["value"] == 4 and sla["retention"]["unit"] == "versions"


def test_the_api_is_declared_as_a_server(env, people):
    servers = {s["server"]: s for s in doc(env)["servers"]}
    assert servers["refexposer-api"]["type"] == "api"
    assert servers["refexposer-api"]["location"].endswith("/api/referentials/people")


def test_what_the_standard_cannot_say_goes_to_custom_properties(env, people):
    """The source, the format and the confidentiality have no ODCS field: inventing one would produce a
    document no other tool can read."""
    custom = {c["property"]: c["value"] for c in doc(env)["customProperties"]}
    assert custom["refexposer.sourceType"] == "local" and custom["refexposer.format"] == "csv"
    assert custom["refexposer.confidential"] is False
    assert custom["refexposer.license"] == "CC-BY-4.0"
    assert custom["refexposer.dataVersion"] == 1
    assert {"url": "https://intranet.example.com/staff", "type": "businessDefinition",
            "description": "Home page of the source"} in doc(env)["authoritativeDefinitions"]


def test_the_contract_is_read_like_the_referential_itself(env, people):
    """Same access rule as everywhere else: a referential an account cannot read does not exist for it."""
    create_user(env, "reader")
    client = new_client(env, "reader", USER_PASSWORD)
    assert client.get("/api/referentials/people/contract").status_code == 404
    env.post("/api/admin/grants", json={
        "referential_id": "people", "level": "read",
        "user_id": next(u["id"] for u in env.get("/api/admin/users").json() if u["username"] == "reader")})
    assert client.get("/api/referentials/people/contract").status_code == 200
    assert client.get("/api/referentials/nope/contract").status_code == 404


def test_declaring_a_contract_does_not_rebuild_anything(env, people):
    """A contract is a promise about the data, not a recipe for it: changing it must not force an update."""
    before = env.get("/api/referentials/people").json()
    definition = {"referentials": [{
        **DEFINITION["referentials"][0],
        "contract": {"version": "3.0.0", "status": "deprecated", "purpose": "Being retired"},
    }]}
    _write(env, definition=definition)
    after = env.get("/api/referentials/people").json()
    assert after["config"]["contract"]["version"] == "3.0.0"
    assert after["version"] == before["version"]  # no new version was published
    assert doc(env)["status"] == "deprecated"
