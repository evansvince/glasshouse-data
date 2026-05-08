#!/usr/bin/env python3
"""
Glasshouse Realty — BoldTrail Agent Sync (Safe)
════════════════════════════════════════════════════════════════

READ-ONLY: This script ONLY makes GET requests to BoldTrail.
It NEVER writes, modifies, deletes, or alters any data in BoldTrail.

What it does:
  1. Reads all active agents from BoldTrail (GET only)
  2. Filters out known test/junk accounts (does NOT deactivate them in BoldTrail)
  3. Detects potential duplicates and agents with missing data
  4. Backs up agents.json before any changes
  5. Merges BoldTrail data with existing agents.json (preserving photos, profileUrls)
  6. Writes reports/flagged-YYYY-MM-DD.json with agents needing review
  7. Never overwrites if BoldTrail returns 0 or suspiciously few agents

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
REPORTS_DIR  = 'reports'
DELAY        = 0.2
MIN_AGENTS   = 50
MIN_PCT_OF_EXISTING = 0.50

# ── JUNK/TEST ACCOUNT FILTERS ─────────────────────────────────────────────────
# Agents matching these patterns are EXCLUDED from agents.json
# They are NOT deactivated in BoldTrail — only flagged for review
JUNK_EMAIL_PATTERNS = [
    r'zillowteam',
    r'test',
    r'\btest\b',
    r'00000',
    r'123456',
    r'sample',
    r'demo@',
    r'dummy',
]
JUNK_NAME_PATTERNS = [
    r'final test',
    r'last time',
    r'test agent',
    r'me myself',
    r'glasshouse test',
    r'dawn test',
    r'final final',
]

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
    if len(d) == 10: return f'({d[:3]}) {d[3:6]}-{d[6:]}'
    return p

def clean_title(t):
    if not t or t.upper() in ('REALTOR', 'AGENT', ''): return 'REALTOR\u00ae'
    return t.replace(' / Agent', '').strip() or 'REALTOR\u00ae'

def infer_region(office):
    if not office: return None
    low = office.lower()
    for kw, region in OFFICE_TO_REGION.items():
        if kw in low: return region
    return None

def is_junk(email, name):
    email_low = email.lower()
    name_low  = name.lower()
    for p in JUNK_EMAIL_PATTERNS:
        if re.search(p, email_low): return True, f'email matches junk pattern: {p}'
    for p in JUNK_NAME_PATTERNS:
        if re.search(p, name_low):  return True, f'name matches junk pattern: {p}'
    return False, ''

def abort(msg):
    print(f"\n{'='*60}")
    print(f"ABORT: {msg}")
    print(f"Existing {OUT_FILE} has NOT been modified.")
    print(f"BoldTrail data has NOT been modified.")
    print(f"{'='*60}")
    sys.exit(1)

# ── BOLDTRAIL FETCH (READ ONLY) ───────────────────────────────────────────────
def fetch_bt_agents():
    """
    Single GET request — BoldTrail v1 returns all agents at once.
    READ ONLY — no data is modified in BoldTrail.
    """
    print("\n── BoldTrail Fetch (GET requests only) ─────────────────")
    print("  GET /v1/users...", end=' ', flush=True)
    url = f"{BT_BASE}/users?api_key={BT_API_KEY}&full_info=1&status=active"
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as r:
            agents = json.loads(r.read().decode())
        if not isinstance(agents, list):
            abort(f"Unexpected BoldTrail response format: {type(agents)}")
        print(f"{len(agents)} agents returned")
        return agents
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            abort(f"BoldTrail authentication failed (HTTP {e.code}). Check BOLDTRAIL_API_KEY.")
        abort(f"BoldTrail HTTP error {e.code}.")
    except Exception as e:
        abort(f"BoldTrail fetch error: {e}")

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

# ── FILTER & FLAG ─────────────────────────────────────────────────────────────
def filter_and_flag(agents):
    """
    Separate valid agents from junk/test accounts.
    Detect duplicates and agents with missing data.
    Returns (valid, flagged_report)
    """
    valid    = []
    flagged  = []
    name_map = {}  # name -> list of emails, for duplicate detection

    for a in agents:
        junk, reason = is_junk(a['email'], a['name'])
        if junk:
            flagged.append({
                'name':        a['name'],
                'email':       a['email'],
                'boldtrailId': a['boldtrailId'],
                'flag':        'JUNK_ACCOUNT',
                'reason':      reason,
                'action':      'Excluded from agents.json. Review in BoldTrail BackOffice.',
            })
            continue

        # Agents/team leaders with no region are excluded from the site
        if not a['regions']:
            flagged.append({
                'name':        a['name'],
                'email':       a['email'],
                'boldtrailId': a['boldtrailId'],
                'office':      a['office'],
                'team':        a['team'],
                'flag':        'NO_REGION',
                'reason':      'No region assigned in BoldTrail',
                'action':      'Excluded from site. Assign a region in BoldTrail to make visible.',
            })
            continue

        # Check for other missing data (agent still shown on site)
        issues = []
        if not a['office']:     issues.append('no_office')
        if not a['phone']:      issues.append('no_phone')
        if not a['team']:       issues.append('no_team')

        if issues:
            flagged.append({
                'name':        a['name'],
                'email':       a['email'],
                'boldtrailId': a['boldtrailId'],
                'office':      a['office'],
                'regions':     a['regions'],
                'team':        a['team'],
                'flag':        'MISSING_DATA',
                'reason':      ', '.join(issues),
                'action':      'Agent shown on site. Update their profile in BoldTrail.',
            })

        # Track for duplicate detection
        name_key = re.sub(r'[^a-z]', '', a['name'].lower())
        name_map.setdefault(name_key, []).append(a['email'])

        valid.append(a)

    # Flag potential duplicates (same name, different email)
    for name_key, emails in name_map.items():
        if len(emails) > 1:
            for email in emails:
                flagged.append({
                    'name':   next(a['name'] for a in valid if a['email'] == email),
                    'email':  email,
                    'flag':   'POTENTIAL_DUPLICATE',
                    'reason': f'Same name found with {len(emails)} different emails: {", ".join(emails)}',
                    'action': 'Verify these are different agents or merge/deactivate in BoldTrail.',
                })

    return valid, flagged

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

def prune_backups(keep=30):
    if not os.path.exists(BACKUP_DIR): return
    files = sorted(os.listdir(BACKUP_DIR), reverse=True)
    by_prefix = {}
    for f in files:
        prefix = f.rsplit('_', 3)[0] if '_' in f else f
        by_prefix.setdefault(prefix, []).append(f)
    removed = 0
    for prefix, flist in by_prefix.items():
        for old in flist[keep:]:
            os.remove(os.path.join(BACKUP_DIR, old))
            removed += 1
    if removed:
        print(f"  Pruned {removed} old backups")

# ── LOAD EXISTING ─────────────────────────────────────────────────────────────
def load_existing(filepath):
    if not os.path.exists(filepath): return {}, {}, 0
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
            agent['photo']      = existing.get('photo', '')
            agent['profileUrl'] = existing.get('profileUrl', '')
            agent['loftyId']    = existing.get('loftyId', '')
            # Preserve hidden flag in both directions (true or false)
            if 'hidden' in existing:
                agent['hidden'] = existing['hidden']
            if not agent['teamLogo'] and existing.get('teamLogo'):
                agent['teamLogo'] = existing['teamLogo']
            if existing.get('source') == 'both':
                agent['source'] = 'both'
            updated_count += 1
        else:
            new_count += 1
            print(f"  ✦ NEW: {agent['name']} <{agent['email']}> region:{agent['regions']}")
        merged.append(agent)
    print(f"  Updated: {updated_count} | New: {new_count}")

    # Preserve Cleveland agents from existing data — not in Dayton BoldTrail
    # These will be preserved as-is until Cleveland BoldTrail is connected
    bt_emails = {a['email'] for a in merged}
    for email, existing in by_email.items():
        if email in bt_emails:
            continue
        if 'Cleveland' in existing.get('regions', []):
            merged.append(existing)
            print(f"  ✦ PRESERVED Cleveland: {existing['name']}")

    return merged

# ── WRITE REPORTS ─────────────────────────────────────────────────────────────
def write_report(flagged, merged, dry_run=False):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    date_str  = datetime.now().strftime('%Y-%m-%d')
    generated = datetime.now().isoformat()
    dr_label  = ' (dry run)' if dry_run else ''

    junk       = [f for f in flagged if f['flag'] == 'JUNK_ACCOUNT']
    no_region  = [f for f in flagged if f['flag'] == 'NO_REGION']
    duplicates = [f for f in flagged if f['flag'] == 'POTENTIAL_DUPLICATE']
    missing    = [f for f in flagged if f['flag'] == 'MISSING_DATA']

    summary = {
        'total_from_boldtrail': len(merged) + len(junk) + len(no_region),
        'live_on_site':         len([a for a in merged if not a.get('hidden')]),
        'hidden_on_site':       len([a for a in merged if a.get('hidden')]),
        'excluded_no_region':   len(no_region),
        'excluded_junk':        len(junk),
        'missing_data':         len(missing),
        'potential_duplicates': len(duplicates),
    }

    # ── 1. Live agents report ──────────────────────────────────────────────────
    live_path = os.path.join(REPORTS_DIR, f'live-agents-{date_str}.json')
    live_agents = [{
        'name':        a['name'],
        'email':       a['email'],
        'regions':     a['regions'],
        'team':        a.get('team', ''),
        'office':      a.get('office', ''),
        'phone':       a.get('phone', ''),
        'has_photo':   bool(a.get('photo')),
        'has_profile': bool(a.get('profileUrl')),
        'hidden':      a.get('hidden', False),
        'source':      a.get('source', ''),
        'boldtrailId': a.get('boldtrailId', ''),
        'loftyId':     a.get('loftyId', ''),
    } for a in sorted(merged, key=lambda x: x['name'].lower())]

    with open(live_path, 'w') as f:
        json.dump({'generated': generated, 'dry_run': dry_run,
                   'summary': summary, 'agents': live_agents}, f, indent=2)
    print(f"  Live agents:   {live_path}{dr_label} ({len(live_agents)} agents)")

    # ── 2. No-region report ────────────────────────────────────────────────────
    nr_path = os.path.join(REPORTS_DIR, f'no-region-{date_str}.json')
    with open(nr_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': 'Agents excluded from site — no region in BoldTrail. Assign a region to make them visible.',
            'count':       len(no_region),
            'agents':      sorted(no_region, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No region:     {nr_path}{dr_label} ({len(no_region)} agents)")

    # ── 3. Flagged/junk report ─────────────────────────────────────────────────
    flagged_path = os.path.join(REPORTS_DIR, f'flagged-{date_str}.json')
    with open(flagged_path, 'w') as f:
        json.dump({
            'generated':            generated,
            'dry_run':              dry_run,
            'summary':              summary,
            'junk_accounts':        junk,
            'potential_duplicates': duplicates,
            'missing_data':         missing,
        }, f, indent=2)
    print(f"  Flagged:       {flagged_path}{dr_label}")

    # ── Console summary ────────────────────────────────────────────────────────
    print(f"\n── Report Summary ──────────────────────────────────────")
    print(f"  Live on site:          {summary['live_on_site']}")
    print(f"  Hidden on site:        {summary['hidden_on_site']}")
    print(f"  Excluded (no region):  {summary['excluded_no_region']}")
    print(f"  Excluded (junk):       {summary['excluded_junk']}")
    print(f"  Missing data:          {summary['missing_data']} (still shown)")
    print(f"  Potential duplicates:  {summary['potential_duplicates']}")

    if junk:
        print(f"\n  Junk/test excluded:")
        for j in junk:
            print(f"    {j['name']} <{j['email']}> — {j['reason']}")

    if duplicates:
        print(f"\n  Potential duplicates:")
        seen = set()
        for d in duplicates:
            if d['reason'] not in seen:
                print(f"    {d['name']}: {d['reason']}")
                seen.add(d['reason'])

    if no_region:
        print(f"\n  No-region (first 10):")
        for a in no_region[:10]:
            print(f"    {a['name']} <{a['email']}> office:{a.get('office') or '(blank)'}")
        if len(no_region) > 10:
            print(f"    ... and {len(no_region)-10} more — see {nr_path}")

    return {'summary': summary}


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--out', default=OUT_FILE)
    args = parser.parse_args()

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('Glasshouse Agent Sync — BoldTrail (READ ONLY)')
    print(f'Timestamp: {ts}')
    print(f'Mode:      {("DRY RUN — no files will be written" if args.dry_run else "LIVE")}')
    print('=' * 60)
    print('NOTE: GET requests only. BoldTrail is never modified.')
    print('=' * 60)

    if not BT_API_KEY:
        abort('BOLDTRAIL_API_KEY not set.')

    # 1. Fetch
    bt_raw = fetch_bt_agents()

    # 2. Parse
    parsed = [a for a in (parse_bt(r) for r in bt_raw) if a]
    print(f"  Parsed {len(parsed)} valid records")

    # 3. Filter junk + flag issues
    print(f"\n── Filtering & Flagging ─────────────────────────────────")
    valid, flagged = filter_and_flag(parsed)
    print(f"  Valid agents:   {len(valid)}")
    print(f"  Flagged total:  {len(flagged)}")

    # 4. Safety checks
    print(f"\n── Safety Checks ───────────────────────────────────────")
    if not valid:
        abort('0 valid agents after filtering — aborting.')
    if len(valid) < MIN_AGENTS:
        abort(f'Only {len(valid)} valid agents (minimum: {MIN_AGENTS}).')
    print(f"  ✓ {len(valid)} valid agents")

    # 5. Load existing
    print(f"\n── Existing Data ───────────────────────────────────────")
    by_email, by_btid, existing_count = load_existing(args.out)
    if existing_count > 0:
        pct = len(valid) / existing_count
        if pct < MIN_PCT_OF_EXISTING:
            abort(f'{len(valid)} valid agents vs {existing_count} existing ({pct:.0%}) — aborting.')
        print(f"  ✓ {len(valid)} new vs {existing_count} existing ({pct:.0%}) — safe")

    # 6. Merge
    print(f"\n── Merging ─────────────────────────────────────────────")
    merged = merge(valid, by_email, by_btid)
    merged.sort(key=lambda a: (0 if a.get('photo') else 1, a['name'].lower()))

    # 7. Report
    report = write_report(flagged, merged, dry_run=args.dry_run)

    # 8. Stats
    visible     = [a for a in merged if not a.get('hidden')]
    with_photo  = [a for a in visible if a.get('photo')]
    with_region = [a for a in visible if a.get('regions')]

    print(f"\n── Final Stats ─────────────────────────────────────────")
    print(f"  Total agents on site:  {len(merged)}")
    print(f"  Visible:               {len(visible)}")
    print(f"  With photo:            {len(with_photo)}")
    print(f"  With region:           {len(with_region)}")
    print(f"  No region (All Ohio):  {len(visible) - len(with_region)}")

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN COMPLETE — no files written")
        print(f"{'='*60}")
        # Output report summary as env var for GitHub Action notification
        summary = report['summary']
        print(f"\nREPORT_SUMMARY=included:{summary['included_in_site']} junk:{summary['excluded_junk']} no_region:{summary['missing_region']} duplicates:{summary['potential_duplicates']}")
        return

    # 9. Backup
    print(f"\n── Backup ──────────────────────────────────────────────")
    backup(args.out)
    prune_backups(keep=30)

    # 10. Write agents.json
    print(f"\n── Writing {args.out} ──────────────────────────────────")
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(merged, f, separators=(',', ':'), ensure_ascii=False)
    print(f"  ✓ {args.out} ({os.path.getsize(args.out):,} bytes, {len(merged)} agents)")

    print(f"\n{'='*60}")
    print(f"SYNC COMPLETE — {len(merged)} agents on site")
    print(f"BoldTrail accessed READ ONLY. Nothing was modified.")
    print(f"Report: {REPORTS_DIR}/flagged-{datetime.now().strftime('%Y-%m-%d')}.json")
    print(f"{'='*60}")

    # Output for GitHub Action step summary
    summary = report['summary']
    print(f"\nREPORT_SUMMARY=included:{summary['included_in_site']} junk:{summary['excluded_junk']} no_region:{summary['missing_region']} duplicates:{summary['potential_duplicates']}")

if __name__ == '__main__':
    main()
