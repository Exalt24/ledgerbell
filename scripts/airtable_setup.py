# LIFESPAN: permanent
"""Creates the Airtable table the run log writes to: "Ledgerbell run log (test)" with one column per summary field.

    AIRTABLE_TOKEN=... python scripts/airtable_setup.py --base-id appXXXXXXXXXXXXXX      # add the table to a base you already have
    AIRTABLE_TOKEN=... python scripts/airtable_setup.py --workspace wspXXXXXXXXXXXXXX    # or make a base of the same name in a workspace

It uses Airtable's Metadata API (POST /v0/meta/bases and POST /v0/meta/bases/{baseId}/tables), so the personal access
token needs the schema.bases:read and schema.bases:write scopes plus data.records:write for the log itself. It is
idempotent: a table or base that already has the name is reused. Prints one JSON line with the base id and the table
id, which go into config.toml under [airtable]. The token comes from AIRTABLE_TOKEN or the first line of the file named
in AIRTABLE_TOKEN_FILE; nothing secret is written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

NAME = "Ledgerbell run log (test)"
API = "https://api.airtable.com"
FIELDS = [
    {"name": "Routine", "type": "singleLineText"},  # the first field is the primary field and must be text
    {"name": "Run at", "type": "dateTime", "options": {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}, "timeZone": "utc"}},
    {"name": "Status", "type": "singleLineText"},
    {"name": "Orders suggested", "type": "number", "options": {"precision": 0}},
    {"name": "Orders placed", "type": "number", "options": {"precision": 0}},
    {"name": "Orders refused", "type": "number", "options": {"precision": 0}},
    {"name": "Mode", "type": "singleLineText"},
    {"name": "Summary", "type": "multilineText"},
]


def token() -> str:
    value = os.environ.get("AIRTABLE_TOKEN", "").strip()
    path = os.environ.get("AIRTABLE_TOKEN_FILE")
    if not value and path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            value = fh.readline().strip()
    if not value:
        sys.exit("missing AIRTABLE_TOKEN (or AIRTABLE_TOKEN_FILE)")
    return value


def call(method: str, path: str, body: dict | None = None, base_url: str = API) -> tuple[int, dict]:
    resp = requests.request(method, base_url + path, json=body, headers={"Authorization": f"Bearer {token()}"}, timeout=30)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def ensure_table(base_id: str) -> str:
    status, data = call("GET", f"/v0/meta/bases/{base_id}/tables")
    if status != 200:
        sys.exit(f"could not read the base's tables ({status}): {data}")
    found = next((t for t in data["tables"] if t["name"] == NAME), None)
    if found:
        return found["id"]
    status, data = call("POST", f"/v0/meta/bases/{base_id}/tables", {"name": NAME, "fields": FIELDS})
    if status != 200:
        sys.exit(f"could not create the table ({status}): {data}")
    return data["id"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base-id", help="add the table to this base")
    ap.add_argument("--workspace", help="make a base named like the table in this workspace")
    args = ap.parse_args()
    base_id = args.base_id
    if not base_id:
        status, data = call("GET", "/v0/meta/bases")
        existing = next((b for b in data.get("bases", []) if b["name"] == NAME), None)
        if existing:
            base_id = existing["id"]
        elif args.workspace:
            status, data = call("POST", "/v0/meta/bases", {"name": NAME, "workspaceId": args.workspace, "tables": [{"name": NAME, "fields": FIELDS}]})
            if status != 200:
                sys.exit(f"could not create the base ({status}): {data}")
            base_id = data["id"]
        else:
            sys.exit("pass --base-id (add the table to a base) or --workspace (make a base)")
    print(json.dumps({"baseId": base_id, "tableId": ensure_table(base_id), "table": NAME}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
