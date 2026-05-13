#!/usr/bin/env python3
"""
Seed agents-test.json from the current live agents.json.

Run this once when setting up the sync-testing branch. It downloads the
live agents.json from GitHub Pages and writes it locally as agents-test.json.
The patched sync script will use this file as the merge baseline so test
runs see realistic data shape.

Usage:
  cd glasshouse-data && git checkout sync-testing
  python3 seed-test-data.py
  git add agents-test.json
  git commit -m "Seed agents-test.json from live data"
  git push origin sync-testing
"""

import json, os, sys, urllib.request

LIVE_URL = 'https://evansvince.github.io/glasshouse-data/agents.json'
OUT_FILE = 'agents-test.json'

def main():
    if os.path.exists(OUT_FILE):
        resp = input(f"{OUT_FILE} already exists. Overwrite? [y/N] ").strip().lower()
        if resp != 'y':
            print("Aborted.")
            sys.exit(0)

    print(f"Fetching {LIVE_URL} ...")
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"FATAL: could not fetch live data: {e}")
        sys.exit(1)

    if not isinstance(data, list):
        print(f"FATAL: expected a list, got {type(data)}")
        sys.exit(1)

    visible = [a for a in data if not a.get('hidden')]
    hidden  = [a for a in data if a.get('hidden')]
    cleveland = [a for a in data if 'Cleveland' in a.get('regions', [])]
    dayton    = [a for a in data if 'Dayton' in a.get('regions', [])]

    print(f"  Total records:   {len(data)}")
    print(f"  Visible:         {len(visible)}")
    print(f"  Hidden:          {len(hidden)}")
    print(f"  Dayton:          {len(dayton)}")
    print(f"  Cleveland:       {len(cleveland)}")

    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'), ensure_ascii=False)

    size = os.path.getsize(OUT_FILE)
    print(f"\n✓ Wrote {OUT_FILE} ({size:,} bytes)")
    print(f"\nNext steps:")
    print(f"  git add {OUT_FILE}")
    print(f"  git commit -m 'Seed agents-test.json from live data'")
    print(f"  git push origin sync-testing")
    print(f"\nOnce pushed, the test data will be reachable at:")
    print(f"  https://raw.githubusercontent.com/evansvince/glasshouse-data/sync-testing/{OUT_FILE}")

if __name__ == '__main__':
    main()
