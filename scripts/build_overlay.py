#!/usr/bin/env python3
"""
Build the Menyoo model-name overlay from the DurtyFree data dumps.

The overlay is delta-only: it contains models that exist upstream (or that we
could infer a name for) but are missing from Menyoo's curated baseline files.
Memory decides which models exist at runtime; this file only supplies names.

Inputs
  --upstream  checkout of DurtyFree/gta-v-data-dumps (needs peds.json, ObjectList.ini)
  --baseline  Menyoo's menyooStuff dir (needs PedList.xml, PropList.txt)
  --unknown   optional text file, one hex hash per line (# comments allowed),
              of ped hashes seen in game that no list can name

Output
  <out>/models.json
  {"revision": N,
   "peds":     [[hash, name, caption, dlc, category], ...],
   "props":    [name, ...],
   "inferred": [[hash, name, caption, dlc, category], ...]}   # dictionary-guessed names

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
    """Returns (category names, set of name hashes, set of lowercase names)."""
    root = ET.parse(path).getroot()
    cats, hashes, names = set(), set(), set()
    for cat in root:
        cname = cat.get("name")
        if cname:
            cats.add(cname)
        for e in cat:
            n = e.get("name")
            if n:
                hashes.add(joaat(n))
                names.add(n.lower())
    return cats, hashes, names


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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--unknown")
    args = ap.parse_args()

    # Baseline (Menyoo's curated files)
    cats, base_ped_hashes, base_ped_names = load_baseline_peds(
        os.path.join(args.baseline, "PedList.xml"))
    base_props = read_lines(os.path.join(args.baseline, "PropList.txt"))
    base_prop_hashes = {joaat(p) for p in base_props}

    # Upstream
    up_peds = load_upstream_peds(os.path.join(args.upstream, "peds.json"))
    up_objects = read_lines(os.path.join(args.upstream, "ObjectList.ini"))

    print(f"baseline: {len(base_ped_hashes)} peds, {len(base_prop_hashes)} props, "
          f"{len(cats)} categories")
    print(f"upstream: {len(up_peds)} peds, {len(up_objects)} objects")

    for label, value, minimum in (
        ("baseline peds", len(base_ped_hashes), MIN_BASELINE_PEDS),
        ("baseline props", len(base_prop_hashes), MIN_BASELINE_PROPS),
        ("upstream peds", len(up_peds), MIN_UPSTREAM_PEDS),
        ("upstream objects", len(up_objects), MIN_UPSTREAM_OBJECTS),
    ):
        if value < minimum:
            sys.exit(f"FAIL: {label} = {value}, expected at least {minimum}")

    # Peds: upstream entries the baseline lacks
    peds, resolved_hashes = [], set()
    for p in up_peds:
        resolved_hashes.add(p["Hash"])
        name = p["Name"]
        if p["Hash"] in base_ped_hashes or denied(name):
            continue
        caption = ((p.get("TranslatedDirectorName") or {}).get("English") or "").strip()
        peds.append([p["Hash"], name, caption, p.get("DlcName") or "",
                     categorize(name, cats)])
    peds.sort(key=lambda e: (e[4], e[1].lower()))

    # Props: upstream names the baseline lacks
    props = sorted({o for o in up_objects if joaat(o) not in base_prop_hashes},
                   key=str.lower)

    # Inferred: dictionary guesses for hashes nothing else can name
    inferred = []
    if args.unknown and os.path.exists(args.unknown):
        unknown = load_unknown(args.unknown)
        resolved_hashes |= base_ped_hashes | base_prop_hashes | {joaat(o) for o in up_objects}
        todo = unknown - resolved_hashes
        print(f"unknown hashes: {len(unknown)} listed, {len(todo)} unresolved")
        if todo:
            t0 = time.time()
            known = base_ped_names | {p["Name"].lower() for p in up_peds}
            found = search_names(todo, known)
            print(f"dictionary search: {len(found)}/{len(todo)} resolved in "
                  f"{time.time() - t0:.1f}s")
            for h, name in found.items():
                if not denied(name):
                    inferred.append([h, name, "", "", categorize(name, cats)])
            inferred.sort(key=lambda e: (e[4], e[1].lower()))

    content = {"peds": peds, "props": props, "inferred": inferred}

    # Revision only bumps when content changes
    out_path = os.path.join(args.out, "models.json")
    revision = 1
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            prev = json.load(f)
        prev_rev = prev.get("revision", 0)
        prev_content = {k: prev.get(k) for k in content}
        revision = prev_rev if prev_content == content else prev_rev + 1

    os.makedirs(args.out, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"revision": revision, **content}, separators=(",", ":")) + "\n")

    print(f"wrote {out_path}: revision {revision}, {len(peds)} peds, "
          f"{len(props)} props, {len(inferred)} inferred "
          f"({os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    main()
