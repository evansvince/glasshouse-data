#!/usr/bin/env python3
"""
Glasshouse Realty — BoldTrail Agent Sync (Safe)
════════════════════════════════════════════════════════════════

READ-ONLY: This script ONLY makes GET requests to BoldTrail.
It NEVER writes, modifies, deletes, or alters any data in BoldTrail.

What it does:
  1. Reads all active agents from BoldTrail (GET only)
  2. Backs up the current agents.json before making any changes
  3. Merges new BoldTrail data with existing agents.json
     - Preserves: photos, profileUrls, loftyId, hidden flags, teamLogos
     - Updates:   name, phone, team, region, office from BoldTrail
  4. Writes updated agents.json ONLY if BoldTrail returned valid data
  5. Aborts if BoldTrail returns 0 agents or suspiciously few

Safety rules:
  - BoldTrail returns 0 agents → abort, existing file untouched
  - BoldTrail returns < 50% of existing count → abort, file untouched
  - BoldTrail API error → abort, file untouched
  - Backup always created before any write
  - GitHub Action fails visibly on any abort (non-zero exit)

Usage:
  export BOLDTRAIL_API_KEY=your_key
  python3 gh-agents-sync-bt.py            # live run
  python3 gh-agents-sync-bt.py --dry-run  # preview only, no writes
"""

import json, time, os, sys, re, argparse, shutil
import urllib.request, urllib.error
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
BT_API_KEY   = os.environ.get('BOLDTRAIL_API_KEY', '')
BT_BASE      = 'https://my.brokermint.com/api/v1'
OUT_FILE     = 'agents.json'
BACKUP_DIR   = 'backups'
DELAY        = 0.2
MIN_AGENTS   = 50
MIN_PCT_OF_EXISTING = 0.50

TEAM_LOGOS = {
    'Asa Cox Homes':                   'https://evansvince.github.io/glasshouse-data/team-logos/ACH Logos White Background.png',
    'Heather Young Real Estate Group': 'https://evansvince.github.io/glasshouse-data/team-logos/Heather Young Team Logo White Background.png',
    'The Blair Team':                  'https://evansvince.github.io/glasshouse-data/team-logos/Blair Team White Background.png',
    'The Sams Group':                  'https://evansvince.github.io/glasshouse-data/team-logos/The Sams Team White Background.png',
    'Team Keener':                     'https://evansvince.github.io/glasshouse-data/team-logos/Team Keener Logos White Background.png',
    'The Signature Team':              '',
    'Labbato Group':                   'https://evansvince.github.io/glasshouse-data/team-logos/Logo Labbato Team Transparent Background.png',
    'Team Pizzo':                      'https://evansvince.github.io/glasshouse-data/team-logos/The Pizzo Team White Background.png',
    'Stacy Pandy Real Estate Group':   'https://evansvince.github.io/glasshouse-data/team-logos/Pandy Team Logo White Background.png',
    'The Legacy Group':                'https://evansvince.github.io/glasshouse-data/team-logos/Legacy Team Logos White Background.png',
    'The Slemc Team':                  'https://evansvince.github.io/glasshouse-data/team-logos/Slemc Logo White Background.png',
    'Chamberlain Dream Team':          'https://evansvince.github.io/glasshouse-data/team-logos/Chamberlain Dream Team Logos White Background.png',
    'Alyssa Christison Team':          'https://evansvince.github.io/glasshouse-data/team-logos/Christison Team Logo White Background.png',
    'GPS Real Estate Group':           'https://evansvince.github.io/glasshouse-data/team-logos/GPS TEAM WHITE BACKGROUND.png',
    'The Bartos Team':                 '',
    'Admin Team':                      '',
}

OFFICE_TO_REGION = {
    'dayton':       'Dayton',    'huber':       'Dayton',
    'kettering':    'Dayton',    'oakwood':     'Dayton',
    'centerville':  'Dayton',    'beavercreek': 'Dayton',
    'troy':         'Dayton',    'sidney':      'Dayton',
    'springfield':  'Dayton',    'waynesville': 'Dayton',
    'wilmington':   'Dayton',    'cincinnati':  'Cincinnati',
    'cleveland':    'Cleveland', 'painesville': 'Cleveland',
    'madison':      'Cleveland', 'geneva':      'Cleveland',
    'columbus':     'Columbus',
}

# ── HELPERS ──────────────────────────────────────────────────────────────────
def fmt_phone(p):
    if not p: return ''
    d = re.sub(r'\D', '', p)
    if len(d) == 11 and d[0] == '1': d = d[1:]
    if len(d) == 10: return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return p

def clean_title(t):
    if not t or t.upper() in ('REALTOR', 'AGENT', ''): return 'REALTOR®'
    return t.replace(' / Agent', '').strip() or 'REALTOR®'

def infer_region(office):
    if not office: return None
    low = office.lower()
    for kw, region in OFFICE_TO_REGION.items():
        if kw in low: return region
    return None

def abort(msg):
    print(f"\n{'='*60}")
    print(f"ABORT: {msg}")
    print(f"Existing {OUT_FILE} has NOT been modified.")
    print(f"BoldTrail data has NOT been modified.")
    print(f"{'='*60}")
    sys.exit(1)

# ── BOLDTRAIL FETCH (READ ONLY) ───────────────────────────────────────────────
def fetch_bt_agents():
    print("\n── BoldTrail Fetch (GET requests only) ─────────────────")
    agents, page, per_page, errors = [], 1, 100, 0

    while True:
        print(f"  GET /v1/users?page={page}...", end=' ', flush=True)
        url = f"{BT_BASE}/users?api_key={BT_API_KEY}&per_page={per_page}&page={page}&full_info=1&status=active"
        try:
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}")
            errors += 1
            if e.code in (401, 403):
                abort(f"BoldTrail authentication failed (HTTP {e.code}). Check BOLDTRAIL_API_KEY.")
            if e.code == 429:
                print("  Rate limited — waiting 15s...")
                time.sleep(15)
                continue
            if errors > 3:
                abort(f"Too many BoldTrail HTTP errors ({errors}). Protecting existing data.")
            time.sleep(3)
            continue
        except Exception as e:
            print(f"Error: {e}")
            errors += 1
            if errors > 3:
                abort(f"Too many BoldTrail errors ({errors}). Protecting existing data.")
            time.sleep(3)
            continue

        if not batch:
            print("empty — done")
            break

        agents.extend(batch)
        print(f"{len(batch)} agents (total: {len(agents)})")

        if len(batch) < per_page:
            break
        page += 1
        time.sleep(DELAY)

    return agents

# ── PARSE ────────────────────────────────────────────────────────────────────
def parse_bt(bt):
    email = (bt.get('email') or '').lower().strip()
    if not email: return None
    first = bt.get('first_name', '') or ''
    last  = bt.get('last_name', '')  or ''
    name  = f"{first} {last}".strip() or bt.get('name', '')
    if not name: return None

    region_raw = bt.get('Region') or bt.get('region') or ''
    regions = [r.strip() for r in region_raw.split(',') if r.strip()]
    team   = bt.get('Team') or bt.get('team_name') or ''
    office = bt.get('office') or bt.get('office_name') or ''

    if not regions and office:
        inferred = infer_region(office)
        if inferred:
            regions = [inferred]

    return {
        'email':       email,
        'name':        name,
        'phone':       fmt_phone(bt.get('phone') or bt.get('phone_number') or ''),
        'title':       clean_title(bt.get('title') or ''),
        'team':        team,
        'teamLogo':    TEAM_LOGOS.get(team, ''),
        'regions':     regions,
        'office':      office,
        'photo':       '',
        'profileUrl':  '',
        'boldtrailId': str(bt.get('id', '')),
        'loftyId':     '',
        'hidden':      False,
        'source':      'boldtrail',
    }

# ── BACKUP ───────────────────────────────────────────────────────────────────
def backup(filepath):
    if not os.path.exists(filepath):
        print(f"  No existing {filepath} — skipping backup")
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    base = os.path.basename(filepath).replace('.json', '')
    dest = os.path.join(BACKUP_DIR, f"{base}_{ts}.json")
    shutil.copy2(filepath, dest)
    print(f"  Backed up → {dest} ({os.path.getsize(dest):,} bytes)")
    return dest

# ── LOAD EXISTING ─────────────────────────────────────────────────────────────
def load_existing(filepath):
    if not os.path.exists(filepath):
        return {}, {}, 0
    try:
        with open(filepath) as f:
            existing = json.load(f)
        by_email = {a['email'].lower(): a for a in existing if a.get('email')}
        by_btid  = {str(a['boldtrailId']): a for a in existing if a.get('boldtrailId')}
        print(f"  Loaded {len(existing)} existing agents from {filepath}")
        return by_email, by_btid, len(existing)
    except Exception as e:
        print(f"  Warning: could not load {filepath}: {e}")
        return {}, {}, 0

# ── MERGE ────────────────────────────────────────────────────────────────────
def merge(new_agents, by_email, by_btid):
    merged, new_count, updated_count = [], 0, 0

    for agent in new_agents:
        existing = by_email.get(agent['email']) or by_btid.get(agent['boldtrailId'])

        if existing:
            # Always preserve these fields from existing data
            agent['photo']      = existing.get('photo', '')
            agent['profileUrl'] = existing.get('profileUrl', '')
            agent['loftyId']    = existing.get('loftyId', '')
            # Preserve manual hidden override
            if existing.get('hidden') is True:
                agent['hidden'] = True
            # Preserve manually set teamLogo if our map doesn't have it
            if not agent['teamLogo'] and existing.get('teamLogo'):
                agent['teamLogo'] = existing['teamLogo']
            # Preserve source = 'both' if Lofty data was previously merged
            if existing.get('source') == 'both':
                agent['source'] = 'both'
            updated_count += 1
        else:
            new_count += 1
            print(f"  ✦ NEW: {agent['name']} <{agent['email']}>  region: {agent['regions']}")

        merged.append(agent)

    print(f"  Updated existing: {updated_count} | New agents: {new_count}")
    return merged

# ── PRUNE OLD BACKUPS ─────────────────────────────────────────────────────────
def prune_backups(keep=30):
    """Keep only the most recent N backups per file type to avoid repo bloat."""
    if not os.path.exists(BACKUP_DIR):
        return
    files = sorted(os.listdir(BACKUP_DIR), reverse=True)
    by_prefix = {}
    for f in files:
        prefix = '_'.join(f.split('_')[:2]) if '_' in f else f
        by_prefix.setdefault(prefix, []).append(f)
    removed = 0
    for prefix, flist in by_prefix.items():
        for old in flist[keep:]:
            os.remove(os.path.join(BACKUP_DIR, old))
            removed += 1
    if removed:
        print(f"  Pruned {removed} old backups (keeping {keep} most recent per type)")

# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Preview only — no files written')
    parser.add_argument('--out', default=OUT_FILE)
    args = parser.parse_args()

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print("=" * 60)
    print("Glasshouse Agent Sync — BoldTrail (READ ONLY)")
    print(f"Timestamp: {ts}")
    print(f"Mode:      {'DRY RUN — no files will be written' if args.dry_run else 'LIVE'}")
    print(f"Output:    {args.out}")
    print(f"Backups:   {BACKUP_DIR}/")
    print("=" * 60)
    print("IMPORTANT: This script makes GET requests only.")
    print("           No data in BoldTrail will ever be modified.")
    print("=" * 60)

    if not BT_API_KEY:
        abort("BOLDTRAIL_API_KEY environment variable is not set.")

    # 1. Fetch
    bt_raw = fetch_bt_agents()

    # 2. Safety checks
    print(f"\n── Safety Checks ───────────────────────────────────────")
    if not bt_raw:
        abort("BoldTrail returned 0 agents — possible API issue.")
    if len(bt_raw) < MIN_AGENTS:
        abort(f"BoldTrail returned only {len(bt_raw)} agents (minimum: {MIN_AGENTS}).")
    print(f"  ✓ BoldTrail returned {len(bt_raw)} agents")

    new_agents = [a for a in (parse_bt(r) for r in bt_raw) if a]
    print(f"  ✓ Parsed {len(new_agents)} valid records")

    # 3. Load existing
    print(f"\n── Existing Data ───────────────────────────────────────")
    by_email, by_btid, existing_count = load_existing(args.out)

    if existing_count > 0:
        pct = len(new_agents) / existing_count
        if pct < MIN_PCT_OF_EXISTING:
            abort(
                f"BoldTrail returned {len(new_agents)} agents but {args.out} has {existing_count}. "
                f"That's only {pct:.0%} of existing — aborting to protect data."
            )
        print(f"  ✓ {len(new_agents)} new vs {existing_count} existing ({pct:.0%}) — safe to proceed")

    # 4. Merge
    print(f"\n── Merging ─────────────────────────────────────────────")
    merged = merge(new_agents, by_email, by_btid)
    merged.sort(key=lambda a: (0 if a.get('photo') else 1, a['name'].lower()))

    # 5. Stats
    visible     = [a for a in merged if not a.get('hidden')]
    with_photo  = [a for a in visible if a.get('photo')]
    with_region = [a for a in visible if a.get('regions')]
    no_region   = [a for a in visible if not a.get('regions')]

    print(f"\n── Stats ───────────────────────────────────────────────")
    print(f"  Total:              {len(merged)}")
    print(f"  Visible:            {len(visible)}")
    print(f"  With photo:         {len(with_photo)}")
    print(f"  With region:        {len(with_region)}")
    print(f"  No region (All OH): {len(no_region)}")
    if no_region:
        for a in no_region[:5]:
            print(f"    {a['name']} — office: {a.get('office','—')}")
        if len(no_region) > 5:
            print(f"    ... and {len(no_region)-5} more")

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN COMPLETE — no files written, nothing changed")
        print(f"{'='*60}")
        return

    # 6. Backup
    print(f"\n── Backup ──────────────────────────────────────────────")
    backup(args.out)
    prune_backups(keep=30)

    # 7. Write
    print(f"\n── Writing {args.out} ──────────────────────────────────")
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(merged, f, separators=(',', ':'), ensure_ascii=False)
    print(f"  ✓ Written: {args.out} ({os.path.getsize(args.out):,} bytes, {len(merged)} agents)")

    print(f"\n{'='*60}")
    print(f"SYNC COMPLETE — {len(merged)} agents")
    print(f"BoldTrail was accessed READ ONLY. Nothing was modified.")
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
