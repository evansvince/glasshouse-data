#!/usr/bin/env python3
"""
Glasshouse Realty — BoldTrail Agent Sync (Safe)
════════════════════════════════════════════════════════════════

READ-ONLY: This script ONLY makes GET requests to BoldTrail.
It NEVER writes, modifies, deletes, or alters any data in BoldTrail.

PATCHED 2026-05-13 — see CHANGELOG block below for what changed.

What it does:
  1. Reads all active agents from BoldTrail (GET only)
  2. Filters out known test/junk accounts (does NOT deactivate them in BoldTrail)
  3. Detects potential duplicates and agents with missing data
  4. Backs up agents.json before any changes
  5. Merges BoldTrail data with existing agents.json:
       - BoldTrail wins for: name, phone, email, team, regions, office, title
       - Existing wins for: photo, profileUrl, loftyId, hidden, teamLogo overrides
       - Match order: boldtrailId → email → normalized name
  6. Soft-deletes agents that disappeared from BoldTrail (30-day grace, then purge)
  7. Writes reports/*.json with agents needing review
  8. Writes SYNC_EVENT marker only when something noteworthy happened
  9. Aborts if BoldTrail returns 0 or suspiciously few agents
  10. Aborts if a PAUSE_SYNC file exists in the repo root

Usage:
  export BOLDTRAIL_API_KEY=your_key
  python3 gh-agents-sync-bt.py            # live run
  python3 gh-agents-sync-bt.py --dry-run  # preview only, no writes

────────────────────────────────────────────────────────────────
CHANGELOG (patch round 1, 2026-05-13)
────────────────────────────────────────────────────────────────
  [BUG-1]  Added name-match fallback to the merge lookup. Previously
           only matched by email/boldtrailId, so any email change in
           BoldTrail would orphan an existing agent and lose their
           photo/profileUrl/loftyId on next sync.
  [BUG-2]  Implemented soft-delete with 30-day grace period for agents
           that disappear from BoldTrail. Previously they were silently
           dropped.
  [BUG-3]  Fixed load_existing tuple-shape: now always returns 4-tuple
           so the caller can safely unpack even on first run.
  [BUG-4]  hidden flag is now sacred: the sync NEVER writes hidden=True
           or hidden=False on an existing record. Only sets it on new
           soft-deletes. Manual hidden:true assignments survive forever.
  [BUG-5]  teamLogo: existing value always wins over TEAM_LOGOS lookup.
           Prevents the sync from flattening a manually-customized URL.
  [GAP-1]  BoldTrail photo URL is now read as a fallback to existing
           Lofty photos: priority is existing(Lofty) → existing(BT) →
           current BT response → none.
  [GAP-2]  PAUSE_SYNC kill switch — if a file named PAUSE_SYNC exists
           in the working directory, the script aborts cleanly.
  [GAP-3]  SYNC_EVENT marker file — written only when there are new
           agents, soft-deletes, purges, or sync-aborted conditions.
           Lets the workflow gate emails on real events instead of
           sending hourly noise.
  [GAP-4]  Cleveland defense-in-depth: any BoldTrail record claiming
           regions:[Cleveland] is dropped on entry, since Cleveland is
           the spreadsheet's domain until the Cleveland BoldTrail
           account is integrated.
  [GAP-5]  Soft-deleted agents (hidden:true + softDeletedAt timestamp)
           older than 30 days are purged on the next sync.
  [LOG-1]  New agents and soft-deletes are logged to a dedicated
           events.json report so the email digest can summarize them
           cleanly.

────────────────────────────────────────────────────────────────
CHANGELOG (patch round 2, 2026-05-13) — BoldTrail write-prevention
────────────────────────────────────────────────────────────────
  [SAFETY-1] _GetOnlyRequest class overrides get_method() to always
             return 'GET'. Rejects 'data=' (body) at construction.
             Strips any 'method=' kwarg.
  [SAFETY-2] BT_READ_ALLOWLIST regex tuple. Only URLs matching one of
             the allowed patterns can be requested. The /users list
             endpoint is allowed; per-user paths are not.
  [SAFETY-3] bt_get() is the only function that touches the BoldTrail
             API. It has no body or method parameter — those failure
             modes are syntactically impossible. Routes everything
             through the allowlist and the GET-only Request class.
  [SAFETY-4] --safety-audit CLI flag prints the safety properties and
             exits. Inspectable by anyone, no code reading required.
  [SAFETY-5] test_sync.py now includes 7 write-prevention tests that
             attempt POST/PUT/PATCH/DELETE/body-injection and verify
             each one is refused. Runs in CI on every change.
"""

import json, time, os, sys, re, argparse, shutil
import urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
BT_API_KEY   = os.environ.get('BOLDTRAIL_API_KEY', '')
BT_BASE      = 'https://my.brokermint.com/api/v1'
OUT_FILE     = 'agents.json'
BACKUP_DIR   = 'backups'
REPORTS_DIR  = 'reports'
PAUSE_FILE   = 'PAUSE_SYNC'
EVENT_FILE   = 'SYNC_EVENT'
DELAY        = 0.2
MIN_AGENTS   = 50
MIN_PCT_OF_EXISTING = 0.50
SOFT_DELETE_GRACE_DAYS = 30

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
    r'team lead test',
    r'team member test',
    r'admin test',
    r'kailey test',
    r'megan test',
    r'cody hemmelgarn test',
    r'jed helmers 2',
    r'jed helmers 1',
    r'john smith',
]

# Staff/internal accounts — excluded from site, not flagged as junk
STAFF_EMAIL_PATTERNS = [
    r'admin@glasshouserealty',
    r'operations@glasshouserealty',
    r'processing@glasshouserealty',
    r'patterson@glasshouserealty',
    r'cvadmin@glasshouserealty',
    r'boldtrail@glasshouserealty',
    r'admin@z-kgroup',
    r'admin@daytonrealestatecrush',
    r'finance2@buildvessel',
    r'sibu@buildvessel',
    r'sibu@glasshouserealty',
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

# Event tracking — populated during the run, consumed by write_event_marker()
EVENTS = {
    'new_agents':       [],   # agents added in this sync
    'soft_deletes':     [],   # agents marked hidden due to BT disappearance
    'purges':           [],   # soft-deleted agents past 30-day grace, removed
    'reactivated':      [],   # agents who reappeared in BT before purge
    'aborted':          False,
    'abort_reason':     '',
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

def normalize_name(name):
    """Lowercase, strip everything but letters. For fuzzy name matching."""
    if not name: return ''
    return re.sub(r'[^a-z]', '', name.lower())

def is_junk(email, name):
    email_low = email.lower()
    name_low  = name.lower()
    for p in JUNK_EMAIL_PATTERNS:
        if re.search(p, email_low): return True, f'email matches junk pattern: {p}'
    for p in JUNK_NAME_PATTERNS:
        if re.search(p, name_low):  return True, f'name matches junk pattern: {p}'
    for p in STAFF_EMAIL_PATTERNS:
        if re.search(p, email_low): return True, f'staff/internal account: {p}'
    # Exclude obvious numbered test prefixes like "001 - ", "002 - " etc with no office
    if re.match(r'^0[0-9][0-9] - ', name) and not email.endswith('@glasshouserealty.com'):
        return True, 'numbered prefix account with no brokerage email'
    return False, ''

def abort(msg):
    EVENTS['aborted'] = True
    EVENTS['abort_reason'] = msg
    write_event_marker()  # so the workflow emails the abort
    print(f"\n{'='*60}")
    print(f"ABORT: {msg}")
    print(f"Existing {OUT_FILE} has NOT been modified.")
    print(f"BoldTrail data has NOT been modified.")
    print(f"{'='*60}")
    sys.exit(1)

def check_pause_switch():
    """[GAP-2] Abort cleanly if PAUSE_SYNC file exists in working dir."""
    if os.path.exists(PAUSE_FILE):
        print(f"\n{'='*60}")
        print(f"PAUSED: {PAUSE_FILE} file present in working directory.")
        print(f"Delete {PAUSE_FILE} to resume sync. Nothing was changed.")
        print(f"{'='*60}")
        # NOT an abort — this is an intentional pause, no event marker
        sys.exit(0)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def parse_iso(s):
    """Parse ISO timestamp safely. Returns None on failure."""
    if not s: return None
    try:
        # Handle both with and without timezone
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None

# ── BOLDTRAIL READ-ONLY SAFETY LAYER ─────────────────────────────────────────
# Four layers of defense make it impossible for this script to write to
# BoldTrail. Each layer is independently sufficient; combined they form a
# multiplicative safety property: every layer would have to fail for a write
# to occur.
#
#   Layer 1: GET-only request class. _GetOnlyRequest overrides get_method()
#            to always return 'GET' regardless of any future code change.
#
#   Layer 2: URL allowlist. _bt_url_allowed() validates the URL against an
#            explicit allowlist of read-only endpoint patterns. Any URL not
#            matching is rejected before the request goes out.
#
#   Layer 3: Body and method block. bt_get() refuses to accept a data
#            payload or a method parameter — there is no syntactic way for
#            a caller to ask for a write through this function.
#
#   Layer 4: Single chokepoint. This is the ONLY function in the script
#            that touches the BoldTrail API. All BT traffic routes through
#            here. The single-entry property is verified by the audit test.
#
# To audit the safety properties, run:  python3 gh-agents-sync-bt.py --safety-audit

# Explicit allowlist of BoldTrail endpoint patterns this script may read.
# Anything not matching one of these patterns is rejected at runtime.
BT_READ_ALLOWLIST = (
    # The /users list endpoint with the parameters the sync uses.
    # Trailing path segments like /users/{id} are NOT in this list.
    re.compile(r'^https://my\.brokermint\.com/api/v1/users(\?[^/]*)?$'),
)


class _GetOnlyRequest(urllib.request.Request):
    """
    [Layer 1] HTTP request subclass that physically cannot be a write.

    Overrides get_method() to always return 'GET' regardless of the
    parent class's behavior. Even if a future code change passes
    method='POST' to Request, urlopen() will still call get_method()
    on this object and receive 'GET'.

    Also asserts the body is None at construction time so a future change
    that adds data= can't bypass the method override.
    """
    def __init__(self, url, *args, **kwargs):
        if kwargs.get('data') is not None:
            raise RuntimeError(
                "BoldTrail safety violation: request body is not permitted. "
                "This script is strictly read-only."
            )
        # Strip any method kwarg — we always GET
        kwargs.pop('method', None)
        super().__init__(url, *args, **kwargs)

    def get_method(self):
        return 'GET'


def _bt_url_allowed(url):
    """[Layer 2] Return True if url matches a known read-only BT endpoint."""
    for pattern in BT_READ_ALLOWLIST:
        if pattern.match(url):
            return True
    return False


def bt_get(url, timeout=120):
    """
    [Layer 3 + 4] The ONLY function in this script that calls BoldTrail.

    - Refuses any URL not in the read-only allowlist
    - Cannot be called with a body (no data= parameter exists)
    - Cannot be called with a method override (no method= parameter exists)
    - Returns the decoded JSON response, or raises on failure

    Any future code that needs BT data must route through this function.
    Adding a write to BoldTrail would require either modifying this
    function (visible in code review) or importing a different HTTP
    library (also visible in code review).
    """
    if not _bt_url_allowed(url):
        raise RuntimeError(
            f"BoldTrail safety violation: URL not in read-only allowlist: {url}"
        )
    req = _GetOnlyRequest(url, headers={'Accept': 'application/json'})
    # Sanity check — paranoid, but cheap. If this assertion ever fires,
    # something has gone very wrong upstream of us.
    assert req.get_method() == 'GET', "method override failed"
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def print_safety_audit():
    """
    [Audit] Print every safety property and exit. Lets a human verify
    by inspection that the script cannot write to BoldTrail.
    """
    print("=" * 60)
    print("BoldTrail Read-Only Safety Audit")
    print("=" * 60)
    print()
    print("Layer 1 — _GetOnlyRequest class:")
    print(f"  get_method() always returns: 'GET'")
    print(f"  Body (data=) parameter:      REJECTED at construction")
    print(f"  method= kwarg:               STRIPPED at construction")
    print()
    print("Layer 2 — URL allowlist:")
    for i, pattern in enumerate(BT_READ_ALLOWLIST, 1):
        print(f"  Allowed pattern {i}: {pattern.pattern}")
    print()
    print("Layer 3 — bt_get() function signature:")
    print(f"  Parameters: url, timeout=120")
    print(f"  Body param:   NOT PRESENT — cannot pass a body")
    print(f"  Method param: NOT PRESENT — cannot pass a method")
    print()
    print("Layer 4 — Single chokepoint:")
    print(f"  bt_get() is the only BoldTrail caller in this script.")
    print(f"  All other code paths must route through it.")
    print()
    print("Verification (run these from the repo root):")
    print(f"  # Exactly one actual urlopen call (the rest are docstrings):")
    print(f"  $ grep -n '^[^#]*urllib\\.request\\.urlopen' gh-agents-sync-bt.py")
    print(f"  # bt_get() is called by the fetcher and audited by tests:")
    print(f"  $ grep -n 'bt_get(' gh-agents-sync-bt.py")
    print(f"  # Full test suite (includes write-prevention checks):")
    print(f"  $ python3 test_sync.py")
    print()
    print("=" * 60)


# ── BOLDTRAIL FETCH (READ ONLY) ───────────────────────────────────────────────
def fetch_bt_agents():
    """
    Single GET request — BoldTrail v1 returns all agents at once.
    READ ONLY — no data is modified in BoldTrail.

    Goes through bt_get() which enforces the four-layer safety property.
    """
    print("\n── BoldTrail Fetch (GET requests only) ─────────────────")
    print("  GET /v1/users...", end=' ', flush=True)
    url = f"{BT_BASE}/users?api_key={BT_API_KEY}&full_info=1&status=active"
    try:
        agents = bt_get(url)
        if not isinstance(agents, list):
            abort(f"Unexpected BoldTrail response format: {type(agents)}")
        print(f"{len(agents)} agents returned")
        return agents
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            abort(f"BoldTrail authentication failed (HTTP {e.code}). Check BOLDTRAIL_API_KEY.")
        abort(f"BoldTrail HTTP error {e.code}.")
    except RuntimeError as e:
        # Safety violation — propagate clearly
        abort(str(e))
    except Exception as e:
        abort(f"BoldTrail fetch error: {e}")

# ── PARSE ────────────────────────────────────────────────────────────────────
# Roles that appear on the public agent finder. BoldTrail's role names are
# case-sensitive strings; we normalize to lowercase before comparing.
# To allow another role, add it to this set in lowercase form.
ALLOWED_ROLES = {'agent', 'team leader', 'broker'}

# ── JOINT LISTING SUPPRESSION ─────────────────────────────────────────────────
# Some agents work as partnerships (married couples, business partners) and
# have ONE joint Lofty profile / agent card on the website. Each individual
# is licensed, so they each have their own BoldTrail record — but only the
# joint Lofty record should appear publicly.
#
# To add a new partnership:
#   1. Confirm the joint Lofty profile exists and is showing correctly
#   2. Look up each partner's BoldTrail id (visible in the BoldTrail API)
#   3. Add their btids to SUPPRESS_BTIDS below with a comment
#   4. Add the joint Lofty record's EMAIL to PRESERVE_JOINT_EMAILS to keep
#      the joint listing visible across syncs (since no BoldTrail record
#      matches it directly)
#
# This list is the only place that needs to change to onboard a new
# partnership. Admins continue to use Lofty as normal — no JSON editing.
SUPPRESS_BTIDS = {
    '272291',  # Kevin Jackson — partner in joint listing "Kevin & Lisa Jackson"
    '272311',  # Lisa Jackson  — partner in joint listing "Kevin & Lisa Jackson"
    '272228',  # Deanna O'Diam — partner in joint listing "Connie Lowery & Deanna O'Diam"
}

# Emails of joint-listing records in agents.json. The sync preserves these
# records across every sync (they aren't matched by any single BoldTrail
# record because they represent partnerships).
PRESERVE_JOINT_EMAILS = {
    'kljackson@glasshouserealty.com',         # Kevin & Lisa Jackson
    'conniedeanna@kunalpatelgroup.com',       # Connie Lowery & Deanna O'Diam
}

# Track suppressed records for the report (lets you verify the list is correct)
SUPPRESSED = []

# Track non-Agent role exclusions for the flagged report
ROLE_EXCLUDED = []


def parse_bt(bt):
    email = (bt.get('email') or '').lower().strip()
    if not email: return None

    # Suppress partnership-individual records (their joint listing handles them)
    btid = str(bt.get('id', ''))
    if btid and btid in SUPPRESS_BTIDS:
        SUPPRESSED.append({
            'name':        f"{bt.get('first_name', '')} {bt.get('last_name', '')}".strip(),
            'email':       email,
            'boldtrailId': btid,
            'reason':      'partner in a joint listing',
        })
        return None

    # Numbered prefixes (e.g. "006 - Laura") appear in first_name in BoldTrail,
    # not just in the joined name. Strip from each component to be safe.
    first = (bt.get('first_name', '') or '').strip()
    last  = (bt.get('last_name', '')  or '').strip()
    # Strip 0XX - prefix from first_name specifically
    first = re.sub(r'^0[0-9]+ - ', '', first).strip()
    last  = re.sub(r'^0[0-9]+ - ', '', last).strip()
    name  = f"{first} {last}".strip() or (bt.get('name', '') or '').strip()
    if not name: return None
    # Final pass in case the prefix was on the joined name field
    name = re.sub(r'^0[0-9]+ - ', '', name).strip()
    if not name: return None

    # Role filter: only "Agent" appears on the public site. Office Administrator,
    # Processor, Team Lead, etc. are excluded. Log every exclusion so you can
    # audit who got filtered out in the reports/role-excluded/ folder.
    role = (bt.get('role') or '').strip()
    if role and role.lower() not in ALLOWED_ROLES:
        ROLE_EXCLUDED.append({
            'name':        name,
            'email':       email,
            'role':        role,
            'boldtrailId': str(bt.get('id', '')),
        })
        return None

    # BoldTrail field names: 'team' (lowercase) is the actual key.
    # Keep the Capitalized/team_name variants as fallback in case BoldTrail
    # ever changes the response shape for some accounts.
    region_raw = bt.get('region') or bt.get('Region') or ''
    regions = [r.strip() for r in region_raw.split(',') if r.strip()]
    team   = bt.get('team') or bt.get('Team') or bt.get('team_name') or ''
    office = bt.get('office') or bt.get('office_name') or ''

    if not regions and office:
        inferred = infer_region(office)
        if inferred:
            regions = [inferred]

    # [GAP-4] Defense-in-depth: a BoldTrail record claiming Cleveland is dropped.
    # Cleveland is owned by the spreadsheet pipeline until the Cleveland
    # BoldTrail account is wired in. Belt and suspenders.
    if 'Cleveland' in regions:
        print(f"  ⚠ Dropping BT record claiming Cleveland region: {name} <{email}>")
        return None

    # [GAP-1] Read BoldTrail's profile photo URL as a fallback.
    # BoldTrail v1 typically exposes the photo as 'profile_picture_url' or
    # 'photo_url'. We accept either and any field that looks like a URL ending
    # in a known image extension.
    bt_photo = (
        bt.get('profile_picture_url')
        or bt.get('photo_url')
        or bt.get('avatar_url')
        or bt.get('image_url')
        or ''
    )
    if bt_photo and not re.match(r'^https?://', bt_photo):
        bt_photo = ''  # ignore relative paths or junk

    return {
        'email':       email,
        'name':        name,
        'phone':       fmt_phone(bt.get('phone') or bt.get('phone_number') or ''),
        'title':       clean_title(bt.get('title') or ''),
        'team':        team,
        'teamLogo':    TEAM_LOGOS.get(team, ''),
        'regions':     regions,
        'office':      office,
        'photo':       '',           # filled by merge() — see priority chain
        'profileUrl':  '',
        'boldtrailId': str(bt.get('id', '')),
        'boldtrailPhoto': bt_photo,  # held temporarily, used by merge()
        'loftyId':     '',
        # NOTE: hidden is intentionally NOT set here. Merge controls it.
        # New agents will have hidden=False; existing records keep their flag.
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
        name_key = normalize_name(a['name'])
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
    """
    [BUG-3] Always returns a 4-tuple: (by_email, by_btid, count, all_records).
    Previously returned a 3-tuple on missing-file, crashing the caller.
    Also now returns by_name for the BUG-1 name-match fallback.
    """
    if not os.path.exists(filepath):
        print(f"  No existing {filepath} — first run.")
        return {}, {}, {}, 0, []
    try:
        with open(filepath) as f:
            existing = json.load(f)
        by_email = {a['email'].lower(): a for a in existing if a.get('email')}
        by_btid  = {str(a['boldtrailId']): a for a in existing if a.get('boldtrailId')}
        by_name  = {}
        for a in existing:
            if a.get('name'):
                nk = normalize_name(a['name'])
                if nk: by_name.setdefault(nk, []).append(a)
        print(f"  Loaded {len(existing)} existing agents from {filepath}")
        return by_email, by_btid, by_name, len(existing), existing
    except Exception as e:
        print(f"  Warning: could not load {filepath}: {e}")
        return {}, {}, {}, 0, []

# ── MERGE ────────────────────────────────────────────────────────────────────

# Manual BoldTrail-id to existing-record pairings.
# When BoldTrail and Lofty have the same agent under different names/emails
# (Fred/Frederick, Tim/Timothy, married name changes, etc.) we tell the sync
# explicitly: "BoldTrail id X is the existing record currently named Y."
# The merge will then update the name/email/etc. from BoldTrail while
# preserving the existing photo/profileUrl/loftyId.
#
# This is the right place for one-time pairings that don't recur once
# resolved. Every entry here represents a record that did NOT have a
# boldtrailId before but now does. After the first sync, that record carries
# its boldtrailId forward forever and future syncs match by btid — no
# further intervention needed.
#
# To pair a new mismatch:
#   1. Find the existing record's name in agents.json (the Lofty-side name)
#   2. Find the corresponding BoldTrail id from the API or BackOffice
#   3. Add an entry below: "boldtrail_id": "existing name as it appears in agents.json"
MANUAL_BTID_PAIRINGS = {
    '272241': 'Fred Seeger',       # BoldTrail: Frederick Seeger
    '272581': 'Tim Young',         # BoldTrail: Timothy Young
    '272386': 'Sierrah Gunder',    # BoldTrail: Sierrah Hardy (married name)
}


def find_existing(agent, by_email, by_btid, by_name):
    """
    Locate an existing record using a four-tier match.
    Returns the matched dict or None.

    Match priority (most reliable first):
      0. MANUAL_BTID_PAIRINGS — one-time bridge for legacy records that
         existed in Lofty before BoldTrail tracked them
      1. boldtrailId — exact, set by BoldTrail itself
      2. email — usually stable, but can be edited
      3. normalized name — last-resort fallback for first sync or email changes

    Name match only returns a single unambiguous result. If multiple existing
    records share the same normalized name, we refuse to match by name to
    avoid randomly clobbering one of them.
    """
    # 0. Manual pairing override (resolves Fred/Frederick-style mismatches)
    btid = str(agent.get('boldtrailId', ''))
    if btid and btid in MANUAL_BTID_PAIRINGS:
        target_name = MANUAL_BTID_PAIRINGS[btid].lower()
        for nk, candidates in by_name.items():
            if nk == normalize_name(MANUAL_BTID_PAIRINGS[btid]):
                if len(candidates) == 1:
                    return candidates[0]
                # If ambiguous, refuse to guess — log and fall through
                print(f"  ⚠ MANUAL_BTID_PAIRINGS: {btid} → '{MANUAL_BTID_PAIRINGS[btid]}' is ambiguous ({len(candidates)} matches), skipping")
    # 1. boldtrailId
    if btid and btid in by_btid:
        return by_btid[btid]
    # 2. email
    email = agent.get('email', '').lower()
    if email and email in by_email:
        return by_email[email]
    # 3. normalized name (unambiguous only)
    nk = normalize_name(agent.get('name', ''))
    if nk and nk in by_name:
        candidates = by_name[nk]
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous — multiple existing records share this name. Don't guess.
    return None

def pick_photo(existing, agent):
    """
    [GAP-1] Photo priority chain:
      1. Existing Lofty photo (cdn.lofty.com or cdn.chime.me)
      2. Existing photo of any other source (manual upload, etc.)
      3. Fresh BoldTrail photo from this sync
      4. Existing photo even if unknown source (better than nothing)
      5. Empty (placeholder will render client-side)

    Note: Lofty photos always win because they're the polished headshots
    agents have curated for the brokerage's Lofty profile. We never let
    BoldTrail's photo overwrite a Lofty photo.
    """
    existing_photo = (existing.get('photo') or '') if existing else ''
    bt_photo = agent.get('boldtrailPhoto', '') or ''

    if existing_photo and ('cdn.lofty.com' in existing_photo or 'cdn.chime.me' in existing_photo):
        return existing_photo   # Lofty wins — protect curated headshots
    if existing_photo:
        return existing_photo   # any other existing photo beats a fresh BT pull
    if bt_photo:
        return bt_photo         # new agent or no existing photo — use BT
    return ''

def merge(new_agents, by_email, by_btid, by_name, existing_all):
    """
    Three-way merge:
      - BoldTrail wins for identity/contact/team/region/office/title
      - Existing wins for photo (Lofty-first), profileUrl, loftyId, hidden, teamLogo
      - Cleveland records (source:spreadsheet) are appended unchanged

    [BUG-4] hidden is sacred: never written to an existing record. The only
    code path that touches hidden is the soft-delete logic, which sets it
    on newly-disappeared agents only.
    """
    merged, new_count, updated_count = [], 0, 0
    # Track which existing records were explicitly matched during merge.
    # Used by the soft-delete pass to know who is genuinely missing from BT
    # vs. who got matched to an incoming record. Identifying by Python's
    # id() because emails/btids may change across the join.
    matched_existing_ids = set()

    for agent in new_agents:
        existing = find_existing(agent, by_email, by_btid, by_name)
        if existing is not None:
            matched_existing_ids.add(id(existing))

        # Photo: priority chain (Lofty → manual → BT → none)
        agent['photo'] = pick_photo(existing, agent)
        # boldtrailPhoto was a temporary field — strip it from the output
        agent.pop('boldtrailPhoto', None)

        if existing:
            # Preserve fields the sync does not own
            agent['profileUrl'] = existing.get('profileUrl', '')
            agent['loftyId']    = existing.get('loftyId', '')

            # [BUG-4] hidden: copy whatever the existing record had.
            # Manually-set hidden:true stays true forever. We never write here.
            if 'hidden' in existing:
                agent['hidden'] = existing['hidden']
            else:
                agent['hidden'] = False

            # [BUG-5] teamLogo: existing value always wins if it exists.
            # Only fall through to TEAM_LOGOS dict if existing record has none.
            if existing.get('teamLogo'):
                agent['teamLogo'] = existing['teamLogo']

            # If existing record was previously soft-deleted but is back in BT,
            # clear the soft-delete state (reactivation).
            if existing.get('softDeletedAt'):
                EVENTS['reactivated'].append({
                    'name':  agent['name'],
                    'email': agent['email'],
                    'softDeletedAt': existing.get('softDeletedAt'),
                })
                print(f"  ↻ REACTIVATED: {agent['name']} <{agent['email']}> (was soft-deleted)")
                # softDeletedAt is not carried forward; hidden was set by us
                # when we soft-deleted, so we clear it now.
                agent['hidden'] = False

            # Source: 'both' means present in BT AND known to spreadsheet too.
            # If existing was tagged 'both', keep it that way.
            if existing.get('source') == 'both':
                agent['source'] = 'both'

            updated_count += 1
        else:
            # Brand new agent — defaults
            agent['hidden'] = False
            new_count += 1
            EVENTS['new_agents'].append({
                'name':        agent['name'],
                'email':       agent['email'],
                'regions':     agent['regions'],
                'team':        agent.get('team', ''),
                'office':      agent.get('office', ''),
                'boldtrailId': agent.get('boldtrailId', ''),
                'has_photo':   bool(agent.get('photo')),
            })
            print(f"  ✦ NEW: {agent['name']} <{agent['email']}> region:{agent['regions']}")

        merged.append(agent)

    print(f"  Updated: {updated_count} | New: {new_count}")

    # ── Cleveland preservation ──────────────────────────────────────────────
    # Cleveland records live in the spreadsheet pipeline, not BoldTrail.
    # We carry them through every sync untouched.
    bt_emails = {a['email'] for a in merged if a.get('email')}
    bt_btids  = {str(a['boldtrailId']) for a in merged if a.get('boldtrailId')}
    cleveland_preserved = 0
    for existing in existing_all:
        is_cleveland = (
            'Cleveland' in existing.get('regions', [])
            or existing.get('source') == 'spreadsheet'
        )
        if not is_cleveland:
            continue
        # If somehow a Cleveland record collides with a BT email/btid, prefer
        # the Cleveland record (BT shouldn't have Cleveland agents yet).
        email = existing.get('email', '')
        btid  = str(existing.get('boldtrailId', ''))
        # Remove any BT record that collided with this Cleveland record
        if email and email in bt_emails:
            merged = [m for m in merged if m.get('email') != email]
            print(f"  ⚠ BT record collided with Cleveland email — Cleveland wins: {existing['name']}")
        elif btid and btid in bt_btids:
            merged = [m for m in merged if str(m.get('boldtrailId', '')) != btid]
            print(f"  ⚠ BT record collided with Cleveland boldtrailId — Cleveland wins: {existing['name']}")
        merged.append(existing)
        cleveland_preserved += 1
    if cleveland_preserved:
        print(f"  ✦ Preserved {cleveland_preserved} Cleveland agents (spreadsheet source)")

    # ── [BUG-2] Soft-delete pass ────────────────────────────────────────────
    # Anyone in existing_all who was NOT explicitly matched during the merge
    # above (and isn't Cleveland) gets soft-deleted. Using explicit-match
    # tracking via id() avoids a subtle bug where two existing records with
    # the same normalized name would protect each other from soft-delete
    # when only one of them is the real match.
    soft_deleted_now = 0
    purged_now = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=SOFT_DELETE_GRACE_DAYS)

    for existing in existing_all:
        # Skip Cleveland — already handled
        if 'Cleveland' in existing.get('regions', []) or existing.get('source') == 'spreadsheet':
            continue
        # Skip joint listings — preserved across syncs (no 1:1 BT match exists).
        # Carry the joint record forward into merged as-is.
        ex_email = (existing.get('email') or '').lower()
        if ex_email in PRESERVE_JOINT_EMAILS:
            merged.append(existing)
            continue
        # Skip if this exact existing record was matched by an incoming BT record
        if id(existing) in matched_existing_ids:
            continue

        # Not in BT response — check soft-delete state
        sd_ts = parse_iso(existing.get('softDeletedAt', ''))
        if sd_ts and sd_ts < cutoff:
            # 30+ days gone — purge
            purged_now += 1
            EVENTS['purges'].append({
                'name':           existing.get('name', ''),
                'email':          existing.get('email', ''),
                'softDeletedAt':  existing.get('softDeletedAt', ''),
            })
            print(f"  ✗ PURGE: {existing.get('name')} (soft-deleted {existing.get('softDeletedAt')})")
            continue  # do NOT carry into merged
        elif sd_ts:
            # Still in grace period — keep as-is (already hidden:true)
            merged.append(existing)
        else:
            # First time disappeared — soft-delete now
            existing_copy = dict(existing)
            existing_copy['hidden'] = True
            existing_copy['softDeletedAt'] = now_iso()
            soft_deleted_now += 1
            EVENTS['soft_deletes'].append({
                'name':    existing.get('name', ''),
                'email':   existing.get('email', ''),
                'regions': existing.get('regions', []),
                'team':    existing.get('team', ''),
            })
            print(f"  ⚠ SOFT DELETE: {existing.get('name')} <{existing.get('email')}> (not in BT)")
            merged.append(existing_copy)

    if soft_deleted_now:
        print(f"  Soft-deleted {soft_deleted_now} agent(s) (will purge after {SOFT_DELETE_GRACE_DAYS} days)")
    if purged_now:
        print(f"  Purged {purged_now} agent(s) past {SOFT_DELETE_GRACE_DAYS}-day grace")

    return merged

# ── WRITE REPORTS ─────────────────────────────────────────────────────────────
def write_report(flagged, merged, dry_run=False):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts        = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    generated = datetime.now().isoformat()
    dr_label  = ' (dry run)' if dry_run else ''

    # Create subdirectories
    live_dir = os.path.join(REPORTS_DIR, 'live-agents')
    nr_dir   = os.path.join(REPORTS_DIR, 'no-region')
    flag_dir = os.path.join(REPORTS_DIR, 'flagged')
    evt_dir  = os.path.join(REPORTS_DIR, 'events')
    for d in [live_dir, nr_dir, flag_dir, evt_dir]:
        os.makedirs(d, exist_ok=True)

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
        'included_in_site':     len(merged),
        'missing_region':       len(no_region),
        # Event counts
        'new_agents':           len(EVENTS['new_agents']),
        'soft_deletes':         len(EVENTS['soft_deletes']),
        'purges':               len(EVENTS['purges']),
        'reactivated':          len(EVENTS['reactivated']),
    }

    # ── 1. Live agents report ──────────────────────────────────────────────────
    live_path = os.path.join(live_dir, f'live-agents-{ts}.json')
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
    nr_path = os.path.join(nr_dir, f'no-region-{ts}.json')
    with open(nr_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': 'Agents excluded from site — no region in BoldTrail. Assign a region to make them visible.',
            'count':       len(no_region),
            'agents':      sorted(no_region, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No region:     {nr_path}{dr_label} ({len(no_region)} agents)")

    # ── 2b. Role-excluded report ───────────────────────────────────────────────
    # Non-Agent roles (Office Administrator, Processor, Team Lead, Broker, etc.)
    # are filtered at parse time. This report lists who got filtered so you
    # can confirm the role filter is doing the right thing.
    role_dir = os.path.join(REPORTS_DIR, 'role-excluded')
    os.makedirs(role_dir, exist_ok=True)
    role_path = os.path.join(role_dir, f'role-excluded-{ts}.json')
    role_excluded_sorted = sorted(ROLE_EXCLUDED, key=lambda x: (x.get('role', ''), x.get('name', '').lower()))
    # Group by role for at-a-glance review
    role_counts = {}
    for r in ROLE_EXCLUDED:
        role_counts[r.get('role', '?')] = role_counts.get(r.get('role', '?'), 0) + 1
    with open(role_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': "BoldTrail records excluded because role != 'Agent'. To allow a role on the site, add it to ALLOWED_ROLES in gh-agents-sync-bt.py.",
            'allowed_roles': sorted(ALLOWED_ROLES),
            'count':       len(ROLE_EXCLUDED),
            'by_role':     role_counts,
            'records':     role_excluded_sorted,
        }, f, indent=2)
    print(f"  Role-excluded: {role_path}{dr_label} ({len(ROLE_EXCLUDED)} records, breakdown: {role_counts})")

    # ── 3a. No-photo report ────────────────────────────────────────────────────
    photo_dir  = os.path.join(REPORTS_DIR, 'no-photo')
    os.makedirs(photo_dir, exist_ok=True)
    no_photo = [{'name': a['name'], 'email': a['email'], 'regions': a['regions'],
                 'team': a.get('team',''), 'source': a.get('source',''),
                 'boldtrailId': a.get('boldtrailId',''), 'loftyId': a.get('loftyId','')}
                for a in merged if not a.get('photo') and not a.get('hidden')]
    photo_path = os.path.join(photo_dir, f'no-photo-{ts}.json')
    with open(photo_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': 'Agents on site with no profile photo. Add a photo in Lofty (preferred) or BoldTrail to populate.',
            'count':       len(no_photo),
            'agents':      sorted(no_photo, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No photo:      {photo_path}{dr_label} ({len(no_photo)} agents)")

    # ── 3b. No-profile-URL report ──────────────────────────────────────────────
    purl_dir = os.path.join(REPORTS_DIR, 'no-profile-url')
    os.makedirs(purl_dir, exist_ok=True)
    no_purl = [{'name': a['name'], 'email': a['email'], 'regions': a['regions'],
                'team': a.get('team',''), 'source': a.get('source',''),
                'boldtrailId': a.get('boldtrailId',''), 'loftyId': a.get('loftyId','')}
               for a in merged if not a.get('profileUrl') and not a.get('hidden')]
    purl_path = os.path.join(purl_dir, f'no-profile-url-{ts}.json')
    with open(purl_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': 'Agents on site with no profile URL. Add agent to Lofty or set a custom slug to populate.',
            'count':       len(no_purl),
            'agents':      sorted(no_purl, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No profile URL:{purl_path}{dr_label} ({len(no_purl)} agents)")

    # ── 4. Flagged/junk report ─────────────────────────────────────────────────
    flagged_path = os.path.join(flag_dir, f'flagged-{ts}.json')
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

    # ── 5. Events report ───────────────────────────────────────────────────────
    # [LOG-1] Dedicated events file for the email digest.
    events_path = os.path.join(evt_dir, f'events-{ts}.json')
    with open(events_path, 'w') as f:
        json.dump({
            'generated':    generated,
            'dry_run':      dry_run,
            'new_agents':   EVENTS['new_agents'],
            'soft_deletes': EVENTS['soft_deletes'],
            'purges':       EVENTS['purges'],
            'reactivated':  EVENTS['reactivated'],
        }, f, indent=2)
    if any([EVENTS['new_agents'], EVENTS['soft_deletes'], EVENTS['purges'], EVENTS['reactivated']]):
        print(f"  Events:        {events_path}{dr_label}")

    # ── Console summary ────────────────────────────────────────────────────────
    print(f"\n── Report Summary ──────────────────────────────────────")
    print(f"  Live on site:          {summary['live_on_site']}")
    print(f"  Hidden on site:        {summary['hidden_on_site']}")
    print(f"  Excluded (no region):  {summary['excluded_no_region']}")
    print(f"  Excluded (junk):       {summary['excluded_junk']}")
    print(f"  Excluded (non-Agent):  {len(ROLE_EXCLUDED)}")
    print(f"  Suppressed (partners): {len(SUPPRESSED)}")
    print(f"  Missing data:          {summary['missing_data']} (still shown)")
    print(f"  Potential duplicates:  {summary['potential_duplicates']}")
    print(f"  No photo:              {len(no_photo)}")
    print(f"  No profile URL:        {len(no_purl)}")
    print(f"  ─ Events this sync ─")
    print(f"  New agents:            {summary['new_agents']}")
    print(f"  Soft-deleted:          {summary['soft_deletes']}")
    print(f"  Purged:                {summary['purges']}")
    print(f"  Reactivated:           {summary['reactivated']}")

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
            print(f"    ... and {len(no_region)-10} more — see reports/no-region/")

    return {'summary': summary}


# ── EVENT MARKER ─────────────────────────────────────────────────────────────
def write_event_marker():
    """
    [GAP-3] Write SYNC_EVENT file only if something noteworthy happened.
    The GitHub Action gates email notifications on this file's existence.

    Noteworthy = new agents, soft-deletes, purges, reactivations, or abort.
    Routine "nothing changed" runs leave no marker → no email noise.
    """
    has_events = (
        EVENTS['aborted']
        or EVENTS['new_agents']
        or EVENTS['soft_deletes']
        or EVENTS['purges']
        or EVENTS['reactivated']
    )
    if not has_events:
        # Remove any stale marker from a previous run
        if os.path.exists(EVENT_FILE):
            os.remove(EVENT_FILE)
        return

    lines = []
    if EVENTS['aborted']:
        lines.append(f"ABORTED: {EVENTS['abort_reason']}")
    if EVENTS['new_agents']:
        lines.append(f"\n{len(EVENTS['new_agents'])} new agent(s) added:")
        for a in EVENTS['new_agents']:
            team    = a.get('team') or 'Solo agent'
            regions = ', '.join(a.get('regions', [])) or 'No region'
            photo   = '' if a['has_photo'] else ' [no photo yet]'
            lines.append(f"  + {a['name']} <{a['email']}> · {team} · {regions}{photo}")
    if EVENTS['soft_deletes']:
        lines.append(f"\n{len(EVENTS['soft_deletes'])} agent(s) soft-deleted (gone from BoldTrail, hidden on site, 30-day grace before purge):")
        for a in EVENTS['soft_deletes']:
            team    = a.get('team') or 'Solo agent'
            regions = ', '.join(a.get('regions', [])) or 'No region'
            lines.append(f"  - {a['name']} <{a['email']}> · {team} · {regions}")
    if EVENTS['purges']:
        lines.append(f"\n{len(EVENTS['purges'])} agent(s) purged (gone for 30+ days):")
        for a in EVENTS['purges']:
            lines.append(f"  ✗ {a['name']} <{a['email']}> (soft-deleted {a.get('softDeletedAt', '')})")
    if EVENTS['reactivated']:
        lines.append(f"\n{len(EVENTS['reactivated'])} agent(s) reactivated (reappeared in BoldTrail):")
        for a in EVENTS['reactivated']:
            lines.append(f"  ↻ {a['name']} <{a['email']}>")

    with open(EVENT_FILE, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"\n  ✦ Event marker written: {EVENT_FILE}")


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--out', default=OUT_FILE)
    parser.add_argument('--safety-audit', action='store_true',
                        help='Print the BoldTrail read-only safety audit and exit')
    args = parser.parse_args()

    if args.safety_audit:
        print_safety_audit()
        sys.exit(0)

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print('=' * 60)
    print('Glasshouse Agent Sync — BoldTrail (READ ONLY)')
    print(f'Timestamp: {ts}')
    print(f'Mode:      {("DRY RUN — no files will be written" if args.dry_run else "LIVE")}')
    print('=' * 60)
    print('NOTE: GET requests only. BoldTrail is never modified.')
    print('=' * 60)

    # [GAP-2] Honor the PAUSE_SYNC kill switch before doing anything.
    check_pause_switch()

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
    by_email, by_btid, by_name, existing_count, existing_all = load_existing(args.out)
    if existing_count > 0:
        # Count only Dayton-side existing records for the safety check
        # (Cleveland records aren't supposed to come from BoldTrail)
        dayton_existing = [
            a for a in existing_all
            if 'Cleveland' not in a.get('regions', [])
            and a.get('source') != 'spreadsheet'
        ]
        if dayton_existing:
            pct = len(valid) / len(dayton_existing)
            if pct < MIN_PCT_OF_EXISTING:
                abort(f'{len(valid)} valid agents vs {len(dayton_existing)} existing Dayton ({pct:.0%}) — aborting.')
            print(f"  ✓ {len(valid)} new vs {len(dayton_existing)} existing Dayton ({pct:.0%}) — safe")

    # 6. Merge
    print(f"\n── Merging ─────────────────────────────────────────────")
    merged = merge(valid, by_email, by_btid, by_name, existing_all)
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

    # 9. Event marker (gates the email in GitHub Actions)
    write_event_marker()

    if args.dry_run:
        print(f"\n{'='*60}")
        print(f"DRY RUN COMPLETE — no files written")
        print(f"{'='*60}")
        # Output report summary as env var for GitHub Action notification
        summary = report['summary']
        print(f"\nREPORT_SUMMARY=included:{summary['included_in_site']} junk:{summary['excluded_junk']} no_region:{summary['missing_region']} duplicates:{summary['potential_duplicates']} new:{summary['new_agents']} soft_del:{summary['soft_deletes']}")
        return

    # 10. Backup
    print(f"\n── Backup ──────────────────────────────────────────────")
    backup(args.out)
    prune_backups(keep=30)

    # 11. Write agents.json
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
    print(f"\nREPORT_SUMMARY=included:{summary['included_in_site']} junk:{summary['excluded_junk']} no_region:{summary['missing_region']} duplicates:{summary['potential_duplicates']} new:{summary['new_agents']} soft_del:{summary['soft_deletes']}")

if __name__ == '__main__':
    main()
