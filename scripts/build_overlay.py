#!/usr/bin/env python3
"""
Build the Menyoo model-name overlay from the DurtyFree data dumps.

The output is the FULL list: Menyoo's curated baseline entries (names, captions,
categories) plus anything upstream has that the baseline lacks, plus names we
could infer for unresolved hashes. It is self-sufficient, so it does not matter
how old a user's local menyooStuff files are. Memory decides which models exist
at runtime; this file only supplies names.

Inputs
  --upstream  checkout of DurtyFree/gta-v-data-dumps (needs peds.json, ObjectList.ini,
              weapons.json)
  --baseline  Menyoo's menyooStuff dir (needs PedList.xml, PropList.txt)
  --unknown   optional text file, one hex hash per line (# comments allowed),
              of ped hashes seen in game that no list can name

Output
  <out>/weapons.json  upstream weapon reference (see build_weapons); not yet read by Menyoo
  <out>/models.json (human-readable, one entry per line)
  {"revision": N,
   "peds":  {"<Category>": [{"name":..., "caption":..., "dlc":..., "inferred": true}, ...], ...},
   "props": ["name", ...]}
  "dlc" is omitted when unknown; "inferred" only appears on dictionary-guessed names.
  Baseline entries keep their file order; new entries follow, sorted by name.

Output is deterministic (sorted, no timestamps) and `revision` only increments
when the content changes, so unchanged inputs never produce a commit.
Exits non-zero on any parse or sanity failure so the workflow commits nothing.
"""

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

MASK = 0xFFFFFFFF
FALLBACK_CATEGORY = "Uncategorised"

# Internal models that are not spawnable characters.
DENY_PREFIXES = ("slod_",)
DENY_EXACT = {"mp_headtargets"}

# Minimum input sizes. A broken upstream push (empty/partial file) fails the job.
MIN_BASELINE_PEDS = 900
MIN_BASELINE_PROPS = 15000
MIN_UPSTREAM_PEDS = 900
MIN_UPSTREAM_OBJECTS = 15000


# Upstream weapon Category -> Menyoo weapon category (names as in vCategoryNames).
GROUP_TO_CATEGORY = {
    "GROUP_MELEE": "Melee",
    "GROUP_PISTOL": "Pistols", "GROUP_STUNGUN": "Pistols",
    "GROUP_SMG": "Submachine Guns", "GROUP_MG": "Machine Guns",
    "GROUP_RIFLE": "Assault Rifles", "GROUP_SHOTGUN": "Shotguns",
    "GROUP_SNIPER": "Sniper Rifles", "GROUP_HEAVY": "Heavy Weapons",
    "GROUP_THROWN": "Throwable Weapons",
}
# Groups whose weapons take the standard tint set (matches 107/108 curated weapons).
TINTABLE_GROUPS = {"GROUP_PISTOL", "GROUP_STUNGUN", "GROUP_SMG", "GROUP_MG", "GROUP_RIFLE",
                   "GROUP_SHOTGUN", "GROUP_SNIPER", "GROUP_HEAVY"}
MIN_UPSTREAM_WEAPONS = 100


# ---------------------------------------------------------------- joaat ----

def _feed(h, s):
    for c in s.lower().encode("utf-8"):
        h = (h + c) & MASK
        h = (h + (h << 10)) & MASK
        h ^= h >> 6
    return h


def _final(h):
    h = (h + (h << 3)) & MASK
    h ^= h >> 11
    return (h + (h << 15)) & MASK


def joaat(s):
    return _final(_feed(0, s))


# ------------------------------------------------------------- category ----

_CLASS = {"a": "Ambient", "s": "Scenario", "u": "StoryScenario", "g": "Gang"}
_GENDER = {"m": "Male", "f": "Female"}


def categorize(name, valid_categories):
    """Derive a PedList.xml category from the model name prefix."""
    n = name.lower()
    cat = FALLBACK_CATEGORY
    if n.startswith(("player_", "p_")) or re.fullmatch(r"mp_[mf]_freemode_\d+", n):
        cat = "Player"
    elif n.startswith("hc_") or n.startswith("g_m_m_zombie_"):
        cat = "Others"
    elif n.startswith("a_c_"):
        cat = "Animal"
    elif n.startswith("ig_"):
        cat = "Story"
    elif n.startswith(("cs_", "csb_")):
        cat = "Cutscene"
    elif n.startswith("mp_"):
        cat = "Multiplayer"
    else:
        m = re.match(r"^([asug])_([mf])_", n)
        if m:
            cat = _CLASS[m.group(1)] + _GENDER[m.group(2)]
    return cat if cat in valid_categories else FALLBACK_CATEGORY


def denied(name):
    n = name.lower()
    return n in DENY_EXACT or n.startswith(DENY_PREFIXES)


# -------------------------------------------------------------- loaders ----

def read_lines(path):
    with open(path, encoding="utf-8-sig", errors="ignore") as f:
        return [line.strip() for line in f if line.strip()]


def load_baseline_peds(path):
    """Returns (ordered category names, [(category, name, caption), ...])."""
    root = ET.parse(path).getroot()
    cats, entries = [], []
    for cat in root:
        cname = cat.get("name")
        if not cname:
            continue
        if cname not in cats:
            cats.append(cname)
        for e in cat:
            n = e.get("name")
            if n:
                entries.append((cname, n, e.get("caption") or ""))
    return cats, entries


def load_upstream_peds(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    mismatches = sum(1 for p in data if p["Hash"] != joaat(p["Name"]))
    if mismatches > max(5, len(data) // 100):
        raise ValueError(f"peds.json: {mismatches} Name/Hash mismatches; schema changed?")
    return data


def load_unknown(path):
    out = set()
    for line in read_lines(path):
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(int(line, 16) & MASK)
    return out


# ------------------------------------------------- dictionary name search ----

_SPLIT = re.compile(
    r"^((?:a|g|s|u|cs|csb|ig|mp|slod)_[a-z]+_[a-z]+_|(?:cs|csb|ig|mp|a)_|slod_)(.*?)(_\d+[a-z]?)?$"
)


def search_names(targets, known_names):
    """
    Guess names for unresolved hashes: known prefix + known word + known/numbered
    suffix. Returns {hash: name}. Any string producing the hash spawns the same
    model (the game only uses the hash), so a false match can only mislabel.
    """
    prefixes, bodies = set(), set()
    suffixes = {""} | {f"_{i:02d}" for i in range(1, 13)} | {f"_{i}" for i in range(1, 10)}
    for n in known_names:
        m = _SPLIT.match(n.lower())
        if m:
            prefixes.add(m.group(1))
            bodies.add(m.group(2))
            if m.group(3):
                suffixes.add(m.group(3))
    parts = bodies | {t for b in bodies for t in b.split("_") if t}

    remaining, found = set(targets), {}
    for p in sorted(prefixes):
        hp = _feed(0, p)
        for part in sorted(parts):
            hb = _feed(hp, part)
            for s in sorted(suffixes):
                h = _final(_feed(hb, s) if s else hb)
                if h in remaining:
                    found[h] = p + part + s
                    remaining.discard(h)
        if not remaining:
            break
    return found


# ----------------------------------------------------------------- main ----

def build_weapons(up_weapons):
    """
    Normalised weapon reference from upstream weapons.json: no translations, stats or
    descriptions. Vehicle weapons and weapons without a real English label are left out.
    "category" is the matching Menyoo category ("" when none). "tints" is a guess from
    the upstream group and tint count: it matched Menyoo's curated data for 107 of 108
    weapons, but it is a guess, not upstream data. Labels of internal weapons can be
    unreliable (upstream labels the Garbage Bag and Handcuffs "Knife").
    """
    out = []
    for w in up_weapons:
        if w.get("IsVehicleWeapon"):
            continue
        label = (((w.get("TranslatedLabel") or {}).get("English")) or "").strip()
        if not label or label.lower() == "invalid":
            continue
        group = w.get("Category") or ""
        entry = {"name": w["Name"], "label": label,
                 "category": GROUP_TO_CATEGORY.get(group, ""), "group": group,
                 "dlc": w.get("DlcName") or ""}
        if len(w.get("Tints") or []) >= 32:
            entry["tints"] = "mk2"
        elif group in TINTABLE_GROUPS:
            entry["tints"] = "standard"
        comps = []
        for c in w.get("Components") or []:
            comp = {"caption": (((c.get("TranslatedLabel") or {}).get("English")) or c["Name"]).strip(),
                    "id": c["Name"]}
            if c.get("IsDefault"):
                comp["default"] = True
            comps.append(comp)
        entry["components"] = comps
        out.append(entry)
    out.sort(key=lambda e: (e["category"] == "", e["category"], e["label"].lower()))
    return out


def render_weapons(revision, weapons):
    """Readable JSON: one weapon header per line, one component per line."""
    j = lambda x: json.dumps(x, ensure_ascii=False)
    blocks = []
    for w in weapons:
        head = j({k: v for k, v in w.items() if k != "components"})[:-1]
        if w["components"]:
            comps = ",\n".join("      " + j(c) for c in w["components"])
            blocks.append(f'    {head}, "components": [\n{comps}\n    ]}}')
        else:
            blocks.append(f'    {head}, "components": []}}')
    return (f'{{\n  "revision": {revision},\n  "weapons": [\n'
            + ",\n".join(blocks) + "\n  ]\n}\n")


def make_entry(name, caption, dlc, inferred=False):
    e = {"name": name, "caption": caption}
    if dlc:
        e["dlc"] = dlc
    if inferred:
        e["inferred"] = True
    return e


def render(revision, peds, props):
    """Readable JSON: one ped / prop per line, stable ordering."""
    out = ["{", f'  "revision": {revision},', '  "peds": {']
    cat_blocks = []
    for cat, entries in peds.items():
        rows = ",\n".join("      " + json.dumps(e, ensure_ascii=False) for e in entries)
        cat_blocks.append(f"    {json.dumps(cat)}: [\n{rows}\n    ]")
    out.append(",\n".join(cat_blocks))
    out.append("  },")
    out.append('  "props": [')
    out.append(",\n".join("    " + json.dumps(p, ensure_ascii=False) for p in props))
    out.append("  ]")
    out.append("}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--unknown")
    args = ap.parse_args()

    # Baseline (Menyoo's curated files)
    cat_order, base_entries = load_baseline_peds(os.path.join(args.baseline, "PedList.xml"))
    base_props = read_lines(os.path.join(args.baseline, "PropList.txt"))
    valid_cats = set(cat_order)

    # Upstream
    up_peds = load_upstream_peds(os.path.join(args.upstream, "peds.json"))
    up_objects = read_lines(os.path.join(args.upstream, "ObjectList.ini"))
    dlc_by_hash = {p["Hash"]: (p.get("DlcName") or "") for p in up_peds}

    base_ped_hashes = {joaat(n) for _, n, _ in base_entries}
    base_prop_hashes = {joaat(p) for p in base_props}
    print(f"baseline: {len(base_ped_hashes)} peds, {len(base_prop_hashes)} props, "
          f"{len(cat_order)} categories")
    print(f"upstream: {len(up_peds)} peds, {len(up_objects)} objects")

    for label, value, minimum in (
        ("baseline peds", len(base_ped_hashes), MIN_BASELINE_PEDS),
        ("baseline props", len(base_prop_hashes), MIN_BASELINE_PROPS),
        ("upstream peds", len(up_peds), MIN_UPSTREAM_PEDS),
        ("upstream objects", len(up_objects), MIN_UPSTREAM_OBJECTS),
    ):
        if value < minimum:
            sys.exit(f"FAIL: {label} = {value}, expected at least {minimum}")

    # Peds: baseline first (file order, first duplicate wins), then additions.
    peds = {c: [] for c in cat_order}
    seen = set()
    for cat, name, caption in base_entries:
        h = joaat(name)
        if h in seen:
            continue
        seen.add(h)
        peds[cat].append(make_entry(name, caption, dlc_by_hash.get(h)))

    additions = {}  # category -> [entry]
    n_upstream_new = 0
    for p in up_peds:
        name = p["Name"]
        if p["Hash"] in seen or denied(name):
            continue
        seen.add(p["Hash"])
        caption = ((p.get("TranslatedDirectorName") or {}).get("English") or "").strip()
        additions.setdefault(categorize(name, valid_cats), []).append(
            make_entry(name, caption, p.get("DlcName")))
        n_upstream_new += 1

    # Inferred: dictionary guesses for hashes nothing else can name
    n_inferred = 0
    if args.unknown and os.path.exists(args.unknown):
        unknown = load_unknown(args.unknown)
        resolved = (seen | base_prop_hashes | {joaat(o) for o in up_objects}
                    | {p["Hash"] for p in up_peds})
        todo = unknown - resolved
        print(f"unknown hashes: {len(unknown)} listed, {len(todo)} unresolved")
        if todo:
            t0 = time.time()
            known = {n.lower() for _, n, _ in base_entries} | {p["Name"].lower() for p in up_peds}
            found = search_names(todo, known)
            print(f"dictionary search: {len(found)}/{len(todo)} resolved in "
                  f"{time.time() - t0:.1f}s")
            for h, name in found.items():
                if not denied(name):
                    additions.setdefault(categorize(name, valid_cats), []).append(
                        make_entry(name, "", "", inferred=True))
                    n_inferred += 1

    for cat, entries in additions.items():
        entries.sort(key=lambda e: e["name"].lower())
        peds.setdefault(cat, []).extend(entries)
    peds = {c: v for c, v in peds.items() if v}  # keeps baseline category order

    # Props: baseline + upstream, deduped by hash, sorted
    by_hash = {}
    for name in base_props + up_objects:
        by_hash.setdefault(joaat(name), name)
    props = sorted(by_hash.values(), key=str.lower)
    n_props_new = len(by_hash) - len(base_prop_hashes)

    # Revision only bumps when content changes
    out_path = os.path.join(args.out, "models.json")
    revision = 1
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            prev = json.load(f)
        same = prev.get("peds") == peds and prev.get("props") == props
        revision = prev.get("revision", 0) + (0 if same else 1)

    os.makedirs(args.out, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(revision, peds, props))

    # weapons.json: upstream reference, its own file and revision
    up_weapons = json.load(open(os.path.join(args.upstream, "weapons.json"), encoding="utf-8"))
    if len(up_weapons) < MIN_UPSTREAM_WEAPONS:
        sys.exit(f"FAIL: upstream weapons = {len(up_weapons)}, expected at least "
                 f"{MIN_UPSTREAM_WEAPONS}")
    weapons = build_weapons(up_weapons)
    w_path = os.path.join(args.out, "weapons.json")
    w_rev = 1
    if os.path.exists(w_path):
        with open(w_path, encoding="utf-8") as f:
            prev_w = json.load(f)
        w_rev = prev_w.get("revision", 0) + (0 if prev_w.get("weapons") == weapons else 1)
    with open(w_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_weapons(w_rev, weapons))
    n_comps = sum(len(w["components"]) for w in weapons)
    print(f"wrote {w_path}: revision {w_rev}, {len(weapons)} weapons, {n_comps} components "
          f"({os.path.getsize(w_path)} bytes)")

    n_peds = sum(len(v) for v in peds.values())
    print(f"wrote {out_path}: revision {revision}, {n_peds} peds "
          f"(+{n_upstream_new} upstream, +{n_inferred} inferred), {len(props)} props "
          f"(+{n_props_new}), {os.path.getsize(out_path)} bytes")


if __name__ == "__main__":
    main()
