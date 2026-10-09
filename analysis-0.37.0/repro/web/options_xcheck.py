"""Cross-check the options: config.yaml (options + schema), config.Settings, translations/en.yaml, DOCS.md."""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from app.config import Settings  # noqa: E402

root = Path(".")
manifest = yaml.safe_load((root / "config.yaml").read_text())
schema = set(manifest["schema"])
options = set(manifest["options"])
fields = set(Settings.model_fields)
trans = set(yaml.safe_load((root / "translations/en.yaml").read_text())["configuration"])
docs = (root / "DOCS.md").read_text()

print("schema - fields:", sorted(schema - fields))
print("fields - schema:", sorted(fields - schema))
print("options - schema:", sorted(options - schema))
print("schema - translations:", sorted(schema - trans))
print("translations - schema:", sorted(trans - schema))
# options without a default in `options:` must be optional in schema (end with '?') or be lists
for key in sorted(schema - options):
    spec = manifest["schema"][key]
    if not (isinstance(spec, str) and spec.endswith("?")):
        print("not in options and not optional:", key, spec)
# defaults: config.yaml options vs Settings defaults
for key in sorted(options & fields):
    yd = manifest["options"][key]
    pd = Settings.model_fields[key].default
    if key == "price_table":
        pd = [p.model_dump() for p in pd]
    if isinstance(pd, tuple):
        pd = list(pd)
    if yd != pd and not (isinstance(yd, (int, float)) and isinstance(pd, (int, float)) and float(yd) == float(pd)):
        print("default differs:", key, "yaml=", yd, "py=", pd)
# docs mention: by option key name (code-formatted) or not
docs_lower = docs.lower()
missing_in_docs = [k for k in sorted(fields) if k not in docs]
print("Settings fields never named in DOCS.md by key:", missing_in_docs)
# Options table rows in DOCS (lines 30-127)
table = docs.splitlines()[31:127]
rows = [r.split("|")[1].strip() for r in table if r.startswith("| ") and not r.startswith("| Option") and not r.startswith("|---")]
print("DOCS options table rows:", len(rows))
for r in rows:
    print("   ", r)
