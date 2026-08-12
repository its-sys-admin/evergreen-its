"""Reconcile vendor BOM/manifest documents against the live `material_catalog`.

OPERATOR-RUN, ENTIRELY LOCAL, READ-ONLY against every ITS system. This is the durable form of the
tooling that produced the 2026-08-11 reconciliation — ten vendor BOMs, 666 extracted lines, 218
unique base parts, no *real* overlap with the 37 catalogue rows then live (see the `in_catalogue`
note below) — whose conclusion shipped as migration 0065's 28 equipment rows. That analysis
originally ran from a session scratchpad and could not be re-derived from the repo; this script
closes that gap. Re-running `extract` against the same corpus reproduces 666/218 exactly, and
`migration` reproduces all 28 rows byte-for-byte.

Three stages, each a subcommand, each writing a file the next one reads:

    extract    corpus of vendor documents  ->  lines.json
    reconcile  lines.json + catalog.json   ->  reconciliation.json
    migration  reconciliation.json         ->  SQL VALUES rows for a material_catalog seed

Typical run (paths are examples — nothing is defaulted into the repo):

    python3 scripts/bom_reconcile.py extract \
        --corpus "$HOME/Desktop/evergreen project/manifests" --out /tmp/bom/lines.json
    npx wrangler d1 execute its-safety-portal --remote --json \
        --command "SELECT id, model_id, manufacturer, category, key_specs, active FROM material_catalog" \
        > /tmp/bom/catalog.json
    python3 scripts/bom_reconcile.py reconcile \
        --lines /tmp/bom/lines.json --catalog /tmp/bom/catalog.json --out /tmp/bom/reconciliation.json
    python3 scripts/bom_reconcile.py migration --reconciliation /tmp/bom/reconciliation.json

CUSTOMER DATA NEVER ENTERS THE REPO. The source documents are customer BOMs and so is everything
derived from them line-by-line, exactly as `scripts/eval_manifest_parse.py` treats the manifest
corpus. Every `--out` path is checked against the repo root and the run is refused if it would write
inside it (`_refuse_if_inside_repo`); pass an output directory outside the checkout. The one
deliberate exception is the `migration` stage, which prints hand-curated equipment rows to stdout —
those same values are already committed in migration 0065.

`in_catalogue` IS A CANDIDATE FLAG, NOT A VERDICT. The matcher is deliberately loose — it looks for a
shared distinctive token between a vendor description and a catalogue `model_id`/`manufacturer`. On
the 2026-08-11 corpus it flagged 5 of 218 parts, and all five are false positives: "SERRATED FLANGE
HEX NUT 300 SERIES SS M6-1.0" matched "Series 7 TR1" on the token `SERIES`, and similar. That is why
the recorded conclusion of that reconciliation was "no real overlap with the 37 live rows" even
though this stage reports 5 — a human read the five and rejected them. Always review the hits; a
tightened matcher that silently dropped them would hide the ones that are real.

Reads only. No Smartsheet, Box, Graph, Worker or D1 egress: the catalogue arrives as a JSON file the
operator produced separately, so this script performs no network I/O at all.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Each vendor writes a different BOM. Keyed to the REAL header row of each format, never a guess:
# (filename fragments, kind, header tokens that MUST all be present, vendor label).
SPECS: list[tuple[tuple[str, ...], str, list[str], str]] = [
    (("Bradley", "Brimfield"), "pdf", ["PART NUMBER", "DESCRIPTION", "GROUPING"], "Nevados"),
    (("Roxbury",), "xlsx", ["PART NUMBER", "DESCRIPTION", "BOM CATEGORY"], "Nevados"),
    (("CommunityPowerGroup",), "pdf", ["ITEM", "DESCRIPTION", "QUANTITY"], "GameChange"),
    (("BONACCI",), "pdf", ["PART NUMBER", "DESCRIPTION"], "Terrasmart"),
    (("DeepLake",), "xlsx", ["PART NUMBER", "PART DESCRIPTION"], "Terrasmart"),
    (("KiwiLog",), "xlsx", ["PART NUM", "DESCRIPTION"], "Terrasmart"),
]

# A part number is 5-8 digits, optionally with one or more -SUFFIX groups (-US, -SHIP, -004).
PART_NUMBER_RE = re.compile(r"\d{5,8}(-[A-Za-z0-9]+)*")
# Suffixes that denote a packaging/shipping variant of the SAME physical part, not a distinct one.
VARIANT_SUFFIX_RE = re.compile(r"-(SHIP|US)$", re.IGNORECASE)

# The vendor's own grouping column is more trustworthy than any keyword guess, so it wins first.
GROUP_MAP = {
    "ACTUATOR": "actuator",
    "CABLE": "cable",
    "HARDWARE": "hardware",
    "MODULE MOUNTING": "module_mounting",
    "POSTS": "post",
    "RACKING/KIT": "racking",
    "SCADA & SENSORS": "scada",
    "TORQUE TUBE": "torque_tube",
}

# Fallback only — consulted when the vendor supplied no grouping we recognise.
DESCRIPTION_KEYWORDS: list[tuple[str, str]] = [
    ("TORQUE TUBE", "torque_tube"), ("PILE", "post"), ("BEAM", "post"),
    ("BEARING", "racking"), ("SADDLE", "racking"), ("MODULE", "module_mounting"),
    ("BOLT", "hardware"), ("NUT", "hardware"), ("WASHER", "hardware"), ("SCREW", "hardware"),
    ("CLAMP", "hardware"), ("BRACKET", "racking"), ("CABLE", "cable"), ("WIRE", "cable"),
    ("SENSOR", "scada"), ("GPS", "scada"), ("ANTENNA", "scada"), ("CONTROLLER", "scada"),
    ("ASSY", "racking"), ("ASSEMBLY", "racking"), ("KIT", "racking"), ("DAMPER", "racking"),
    ("MOTOR", "racking motor"), ("GEAR", "racking"),
]

# Hand-curated: ONLY datasheet-level equipment, the subset of the corpus that earns a catalogue row.
# `model_id` prefers a REAL manufacturer model where the vendor description carries one; otherwise
# the vendor part number, which is itself a stable identifier. This list produced migration 0065.
EQUIPMENT: list[tuple[str, str, str, str, str]] = [
    # --- actuators / drives ---
    ("7000727", "PA 16-Y0008-001", "Nevados", "actuator", "Linear actuator, 65 kN"),
    ("7006955", "7006955", "Nevados", "actuator", "Actuator, 18 mm end rod, 240 VAC, 65 kN, 410 mm"),
    ("805001", "HE9C-61MHD-12003RC-DA216", "Terrasmart", "racking motor", "Slew gear & motor assembly"),
    # --- PV module ---
    ("805000", "ST-150R-12CID2", "Terrasmart", "module", "Mono-crystalline module"),
    # --- SCADA / monitoring devices ---
    ("7000083", "7000083", "Nevados", "scada", "Anemometer, RS485 connection"),
    ("7000118", "SR50AT-L15-PT", "Campbell Scientific", "scada", "Snow sensor, tinned leads"),
    ("7000237", "RAD06", "Nevados", "scada", "6-plate solar radiation shield"),
    ("7000248", "7000248", "Nevados", "scada", "Anemoscope (wind direction)"),
    ("7000930", "7000930", "Nevados", "scada", "QP monitoring unit"),
    ("7000932", "WRSS915", "Nevados", "scada", "Wireless master, 915 MHz"),
    ("7001114", "7001114", "Nevados", "scada", "Irradiation sensor"),
    ("7001248", "7001248", "Nevados", "scada", "Antenna, 915 MHz, N-type connector"),
    ("7002284", "7002284", "Nevados", "scada", "QCC controller, outdoor installation"),
    ("7005574", "7005574", "Nevados", "scada", "GPS receiver"),
    ("7005840", "7005840", "Nevados", "scada", "Control unit, SKC, UL listed"),
    ("20436-000", "20436-000", "GameChange Solar", "scada", "GPS assembly"),
    ("21086-100", "SDC V3-R1", "GameChange Solar", "scada", "Solar drive controller, IEC, RR"),
    ("21079-000", "21079-000", "GameChange Solar", "scada", "6x motor control assembly, 100-240 V"),
    ("802569", "802569", "Terrasmart", "scada", "Row controls assembly, dual input"),
    ("802647", "802647", "Terrasmart", "scada", "TerraTrak repeater kit"),
    ("805226", "805226", "Terrasmart", "scada", "Weather station assembly, Solarland pony panel"),
    ("805227", "805227", "Terrasmart", "scada", "Network station assembly, Solarland pony panel"),
    ("802002", "802002", "Terrasmart", "scada", "TerraTrak AC power unit"),
    # --- cable / conductor ---
    ("7000120", "7000120", "Nevados", "cable", "Actuator cable, 4x1.5 mm2 + 3x0.5 mm2"),
    ("7000161", "7000161", "Nevados", "cable", "SKC cable, outdoor rated, 600 VAC, 3x6 mm2"),
    ("7000840", "7000840", "Nevados", "cable", "Coaxial cable, N-type male to SMA male, 50 ohm"),
    ("50001-000", "50001-000", "GameChange Solar", "cable", "Wire, 6 cond 18 AWG, direct burial, 600 V"),
    # --- grounding ---
    ("119940", "WEEB-DSK14", "Terrasmart", "grounding", "Bonding washer, WEEB DSK14"),
]


def _refuse_if_inside_repo(path: Path) -> Path:
    """Every derived artefact is customer data; refuse to write one into the checkout.

    A relative `--out` run from the repo root is the easy mistake, and `.gitignore` would not
    save us — an operator adding `-A` later would commit a customer BOM. Refuse loudly instead.
    """
    resolved = path.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved
    raise SystemExit(
        f"refusing to write {resolved}: it is inside the repo ({REPO_ROOT}).\n"
        "Vendor BOM lines are customer data and must not enter the checkout — "
        "pass an --out path outside it (e.g. /tmp/bom/lines.json)."
    )


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def pdf_rows(path: Path) -> list[tuple[str, list[str]]]:
    """Every table row on every page, labelled with the page it came from."""
    import pdfplumber  # noqa: PLC0415 — heavy optional import, kept off module load

    out: list[tuple[str, list[str]]] = []
    with pdfplumber.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables() or []:
                for row in table:
                    out.append((str(page_no), [_norm(c) for c in row]))
    return out


def xlsx_rows(path: Path) -> list[tuple[str, list[str]]]:
    """Every row of every worksheet, labelled with the sheet it came from."""
    import openpyxl  # noqa: PLC0415 — heavy optional import, kept off module load

    out: list[tuple[str, list[str]]] = []
    book = openpyxl.load_workbook(path, data_only=True, read_only=True)
    for sheet in book.worksheets:
        for row in sheet.iter_rows(values_only=True):
            out.append((sheet.title, [_norm(c) for c in (row or ())]))
    return out


def find_header(rows: list[tuple[str, list[str]]], must_have: list[str]) -> tuple[int | None, dict[str, int]]:
    """Locate the header row by the columns it MUST contain; return (index, {NAME: column})."""
    for i, (_, cells) in enumerate(rows):
        upper = [c.upper() for c in cells]
        if all(any(token in u for u in upper) for token in must_have):
            return i, {c.upper(): j for j, c in enumerate(cells) if c}
    return None, {}


def column(header: dict[str, int], *names: str) -> int | None:
    """First header column whose name contains any of `names`, in the order given."""
    for name in names:
        for key, idx in header.items():
            if name in key:
                return idx
    return None


def cmd_extract(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus).expanduser()
    if not corpus.is_dir():
        raise SystemExit(f"corpus directory not found: {corpus}")
    out = _refuse_if_inside_repo(Path(args.out).expanduser())

    lines: list[dict[str, str]] = []
    skipped: list[str] = []
    for path in sorted(corpus.iterdir()):
        if path.name.startswith("."):
            continue
        spec = next((s for s in SPECS if any(m.lower() in path.name.lower() for m in s[0])), None)
        if spec is None:
            skipped.append(path.name)
            continue
        _, kind, must_have, vendor = spec
        rows = pdf_rows(path) if kind == "pdf" else xlsx_rows(path)
        header_idx, header = find_header(rows, must_have)
        if header_idx is None:
            skipped.append(f"{path.name} (header not found)")
            continue
        c_part = column(header, "PART NUMBER", "PART NUM", "ITEM")
        c_desc = column(header, "PART DESCRIPTION", "DESCRIPTION")
        c_group = column(header, "GROUPING", "BOM CATEGORY")
        c_qty = column(header, "TOTAL QTY", "REQUIRED QTY", "JOB REQ", "QTY", "QUANTITY")
        c_uom = column(header, "UNIT OF MEASURE", "UOM")

        found = 0
        for page, cells in rows[header_idx + 1:]:
            def cell(i: int | None, _cells: list[str] = cells) -> str:
                return _cells[i] if i is not None and i < len(_cells) else ""

            part = cell(c_part)
            if not PART_NUMBER_RE.fullmatch(part):
                continue
            lines.append({
                "source": path.name,
                "vendor": vendor,
                "page": page,
                "part_number": part,
                "base_part": VARIANT_SUFFIX_RE.sub("", part),
                "description": cell(c_desc),
                "grouping": cell(c_group),
                "qty": cell(c_qty),
                "uom": cell(c_uom),
            })
            found += 1
        print(f"{path.name[:50]:53} {vendor:12} {found:5d} lines")

    if skipped:
        print("\nSKIPPED (no matching vendor spec):")
        for name in skipped:
            print(f"  {name}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(lines, indent=1))
    unique = {line["base_part"] for line in lines}
    print(f"\nTOTAL LINES {len(lines)}   UNIQUE BASE PARTS {len(unique)}")
    print(f"wrote {out}")
    return 0


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z0-9.]+", (text or "").upper()) if len(t) > 3}


def _propose_category(groupings: Counter[str], description: str) -> str:
    best_group = groupings.most_common(1)[0][0].upper() if groupings else ""
    if best_group in GROUP_MAP:
        return GROUP_MAP[best_group]
    upper = (description or "").upper()
    for keyword, category in DESCRIPTION_KEYWORDS:
        if keyword in upper:
            return category
    return "other"


def cmd_reconcile(args: argparse.Namespace) -> int:
    lines = json.loads(Path(args.lines).expanduser().read_text())
    raw = json.loads(Path(args.catalog).expanduser().read_text())
    # `wrangler d1 execute --json` wraps results as [{"results": [...]}]; a bare list also works.
    catalog = raw[0]["results"] if isinstance(raw, list) and raw and "results" in raw[0] else raw
    out = _refuse_if_inside_repo(Path(args.out).expanduser())

    parts: dict[str, dict[str, Any]] = {}
    for line in lines:
        part = parts.setdefault(line["base_part"], {
            "vendor": line["vendor"], "descriptions": Counter(), "groupings": Counter(),
            "uoms": Counter(), "sources": set(),
        })
        for field, bucket in (("description", "descriptions"), ("grouping", "groupings"), ("uom", "uoms")):
            if line.get(field):
                part[bucket][line[field]] += 1
        part["sources"].add(line["source"])

    # A real match needs a DISTINCTIVE model token — a common English word in a description would
    # otherwise match half the catalogue and report false overlap.
    indexed = [
        (entry, {
            t for t in _tokens(entry.get("model_id", "")) | _tokens(entry.get("manufacturer", ""))
            if not t.isalpha() or len(t) > 5
        })
        for entry in catalog
    ]

    rows: list[dict[str, Any]] = []
    for part_number, part in parts.items():
        descriptions: Counter[str] = part["descriptions"]
        description = descriptions.most_common(1)[0][0] if descriptions else ""
        description_tokens = _tokens(description)
        hit = next((e for e, strong in indexed if strong & description_tokens), None)
        groupings: Counter[str] = part["groupings"]
        uoms: Counter[str] = part["uoms"]
        rows.append({
            "part_number": part_number,
            "vendor": part["vendor"],
            "description": description,
            "vendor_grouping": groupings.most_common(1)[0][0] if groupings else "",
            "uom": uoms.most_common(1)[0][0] if uoms else "",
            "proposed_category": _propose_category(groupings, description),
            "in_catalogue": hit is not None,
            "catalogue_match": hit["model_id"] if hit else "",
            "sources": sorted(part["sources"]),
            "n_sources": len(part["sources"]),
        })
    rows.sort(key=lambda r: (r["vendor"], r["proposed_category"], r["part_number"]))

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))

    in_catalogue = sum(1 for r in rows if r["in_catalogue"])
    print(f"BOM unique base parts : {len(parts)}")
    print(f"  already in catalogue: {in_catalogue}")
    print(f"  NOT in catalogue    : {len(rows) - in_catalogue}")
    active = sum(1 for c in catalog if c.get("active"))
    print(f"\nlive catalogue rows   : {len(catalog)}  (active={active})")
    print("\nproposed category x vendor:")
    grid: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        grid[row["proposed_category"]][row["vendor"]] += 1
    for category in sorted(grid, key=lambda k: -sum(grid[k].values())):
        total = sum(grid[category].values())
        breakdown = "  ".join(f"{v}={n}" for v, n in sorted(grid[category].items()))
        print(f"  {category:16} {total:4}   {breakdown}")
    shared = sum(1 for r in rows if r["n_sources"] > 1)
    print(f"\nparts appearing in MULTIPLE manifests: {shared} of {len(rows)}")
    print(f"wrote {out}")
    return 0


def _sql_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def cmd_migration(args: argparse.Namespace) -> int:
    """Emit `material_catalog` VALUES rows for the hand-curated equipment subset.

    Prints to stdout rather than writing a file: the output is the curated 28 rows already
    committed as migration 0065, not customer line data.
    """
    reconciliation = {
        r["part_number"]: r
        for r in json.loads(Path(args.reconciliation).expanduser().read_text())
    }
    rows: list[str] = []
    missing: list[str] = []
    for part_number, model_id, manufacturer, category, specs in EQUIPMENT:
        record = reconciliation.get(part_number)
        if record is None:
            missing.append(part_number)
            continue
        sources = json.dumps(sorted(record["sources"]))
        key_specs = f"{specs} · vendor P/N {part_number}"
        rows.append(
            f"  ({_sql_quote(model_id)}, {_sql_quote(manufacturer)}, {_sql_quote(category)}, "
            f"{_sql_quote(key_specs)}, {_sql_quote(sources)})"
        )
    if missing:
        raise SystemExit(
            "part numbers in the curated EQUIPMENT list were not found in the reconciliation: "
            + ", ".join(missing)
            + "\nEither the corpus changed or the curated list needs updating — do not emit a partial seed."
        )
    print(f"-- {len(rows)} equipment rows", file=sys.stderr)
    print(dict(Counter(e[3] for e in EQUIPMENT)), file=sys.stderr)
    print(",\n".join(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="parse a corpus of vendor BOMs into lines.json")
    p_extract.add_argument("--corpus", required=True, help="directory of vendor BOM documents")
    p_extract.add_argument("--out", required=True, help="output lines.json (MUST be outside the repo)")
    p_extract.set_defaults(func=cmd_extract)

    p_reconcile = sub.add_parser("reconcile", help="match extracted lines against material_catalog")
    p_reconcile.add_argument("--lines", required=True, help="lines.json from the extract stage")
    p_reconcile.add_argument("--catalog", required=True, help="material_catalog rows as JSON")
    p_reconcile.add_argument("--out", required=True, help="output reconciliation.json (outside the repo)")
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_migration = sub.add_parser("migration", help="emit curated equipment VALUES rows to stdout")
    p_migration.add_argument("--reconciliation", required=True, help="reconciliation.json")
    p_migration.set_defaults(func=cmd_migration)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
