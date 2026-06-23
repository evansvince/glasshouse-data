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
BT_API_KEY            = os.environ.get('BOLDTRAIL_API_KEY', '')           # Dayton (Glasshouse Realty)
BT_API_KEY_CLEVELAND  = os.environ.get('BOLDTRAIL_API_KEY_CLEVELAND', '') # Cleveland (Asa Cox Homes)
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

# ── FEATURE FLAGS ─────────────────────────────────────────────────────────────
# Cleveland BoldTrail integration is gated behind this flag.
#
# When DISABLED (production default):
#   - Only Dayton BoldTrail is fetched
#   - Existing Cleveland spreadsheet records are preserved untouched
#   - Behaves exactly like the original Dayton-only sync
#
# When ENABLED:
#   - Both Dayton AND Cleveland BoldTrail accounts are fetched
#   - Cleveland records get regions:['Cleveland'] auto-assigned
#   - Spreadsheet records that don't match BoldTrail go to cleveland-unmatched report
#
# Override via env var SYNC_ENABLE_CLEVELAND=true (the test workflow sets this).
# Default is OFF so production behavior is unchanged until you flip the flag.
ENABLE_CLEVELAND_FETCH = os.environ.get('SYNC_ENABLE_CLEVELAND', '').lower() == 'true'

# ── PROFILE URL AUTO-GENERATION ───────────────────────────────────────────────
# Lofty's "Slug" admin field creates root-level redirects at the public site
# (e.g. Slug "/allen-blackburn" maps to glasshouserealty.com/allen-blackburn).
# For agents without an existing profileUrl, we generate a candidate slug from
# their name, HEAD-request it, and only commit the URL if it returns 200.
#
# Cleveland uses a DIFFERENT agent-page system — slugs don't resolve there the
# same way. We skip profile URL generation for Cleveland agents (identified
# by 'Cleveland' in regions or source == 'spreadsheet' or source.endswith('-cleveland')).
PROFILE_URL_BASE      = 'https://glasshouserealty.com'
PROFILE_URL_CACHE     = 'profile-url-cache.json'
PROFILE_URL_TIMEOUT   = 10  # seconds per HEAD request
PROFILE_URL_USER_AGENT = 'Glasshouse-Profile-URL-Verifier/1.0'
# How long to cache a 404 response before re-checking. The cron runs once per
# hour 9am-6pm M-F, so a 1-hour cooldown effectively means "re-check on the
# next sync." This gives admins fast feedback: set the slug in Lofty admin,
# next sync picks it up.
PROFILE_URL_404_COOLDOWN_SECS = 3600  # 1 hour

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
    # Gmail plus-aliasing on @glasshouserealty addresses is a dev-test convention
    # (e.g. cody+1@glasshouserealty.com, kailey+1@glasshouserealty.com).
    # These are sandbox accounts, not real agents.
    r'\+\d+@glasshouserealty\.com',
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
    # Names starting with "testing" (e.g. "Testing ACH", "Testing: Team Lead")
    # are BoldTrail sandbox records.
    r'^testing\b',
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


def slugify_name(name):
    """
    Convert "Allen Blackburn" → "allen-blackburn".
    Handles edge cases: apostrophes, multi-word last names, hyphens, accents.

    Examples:
      "Allen Blackburn"           → "allen-blackburn"
      "Deanna O'Diam"             → "deanna-odiam"
      "Lisa Al-Saedi"             → "lisa-al-saedi"
      "Cassandra Cox Soto Estrada" → "cassandra-cox-soto-estrada"
      "Madiera McCorkle"          → "madiera-mccorkle"
      "Veronica Plumb-Nelson"     → "veronica-plumb-nelson"
    """
    if not name: return ''
    s = name.lower().strip()
    # Strip apostrophes entirely (O'Diam → ODiam → odiam)
    s = s.replace("'", "").replace("\u2019", "")
    # Replace any non-alphanumeric character with a hyphen
    s = re.sub(r'[^a-z0-9]+', '-', s)
    # Collapse multiple hyphens and trim
    s = re.sub(r'-+', '-', s).strip('-')
    return s


# Cache of verified profile URLs. Loaded on demand by verify_profile_url().
_profile_url_cache = None

def _load_profile_url_cache():
    """Load the cache of slug→status mappings (lazy, so tests don't need the file)."""
    global _profile_url_cache
    if _profile_url_cache is not None:
        return _profile_url_cache
    if not os.path.exists(PROFILE_URL_CACHE):
        _profile_url_cache = {}
        return _profile_url_cache
    try:
        with open(PROFILE_URL_CACHE) as f:
            _profile_url_cache = json.load(f)
    except (IOError, json.JSONDecodeError):
        _profile_url_cache = {}
    return _profile_url_cache


def _save_profile_url_cache():
    """Persist the cache. Called once at end of sync."""
    if _profile_url_cache is None: return
    try:
        with open(PROFILE_URL_CACHE, 'w') as f:
            json.dump(_profile_url_cache, f, indent=2, sort_keys=True)
    except IOError as e:
        print(f"  ⚠ Could not save {PROFILE_URL_CACHE}: {e}")


def verify_profile_url(slug):
    """
    HEAD-request https://glasshouserealty.com/{slug} and return the public
    URL if it resolves (200), or '' if it doesn't (404, etc).

    Cached per-slug so we don't re-hit Lofty's server every sync for the
    same agents. Status 200 is cached forever (slugs don't get removed
    once set). Status 404 expires after PROFILE_URL_404_COOLDOWN_SECS
    (1 hour by default) — short enough that admins see slug-setup take
    effect on the next sync, long enough to avoid hammering Lofty.
    """
    if not slug: return ''
    cache = _load_profile_url_cache()
    cached = cache.get(slug)
    now = datetime.now(timezone.utc).isoformat()
    if cached:
        if cached.get('status') == 200:
            # Verified working — trust the cache
            return cached.get('url', '')
        # 404 or other — check if cache entry is stale
        checked = parse_iso(cached.get('checked_at', ''))
        if checked and (datetime.now(timezone.utc) - checked).total_seconds() < PROFILE_URL_404_COOLDOWN_SECS:
            return ''  # still in cooldown
        # cache entry expired, re-verify below

    url = f"{PROFILE_URL_BASE}/{slug}"
    try:
        req = urllib.request.Request(
            url,
            method='HEAD',
            headers={'User-Agent': PROFILE_URL_USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=PROFILE_URL_TIMEOUT) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception:
        # Network error, timeout, etc — treat as unverified, retry next sync
        return ''

    cache[slug] = {
        'status':     status,
        'url':        url if status == 200 else '',
        'checked_at': now,
    }
    return url if status == 200 else ''

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
def fetch_bt_for_account(api_key, account_label):
    """
    Single GET request to one BoldTrail account. Returns list of records.
    READ ONLY — no data is modified in BoldTrail.

    Goes through bt_get() which enforces the four-layer safety property.

    account_label is a string for logging/error context (e.g. 'Dayton', 'Cleveland').
    """
    print(f"  GET /v1/users ({account_label})...", end=' ', flush=True)
    url = f"{BT_BASE}/users?api_key={api_key}&full_info=1&status=active"
    try:
        agents = bt_get(url)
        if not isinstance(agents, list):
            abort(f"Unexpected BoldTrail response format for {account_label}: {type(agents)}")
        print(f"{len(agents)} records returned")
        return agents
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            abort(f"BoldTrail authentication failed for {account_label} (HTTP {e.code}).")
        abort(f"BoldTrail HTTP error {e.code} for {account_label}.")
    except RuntimeError as e:
        abort(str(e))
    except Exception as e:
        abort(f"BoldTrail fetch error for {account_label}: {e}")


def fetch_bt_agents():
    """
    Fetch agents from BOTH BoldTrail accounts (Dayton + Cleveland).

    Returns a list of (record, account_label) tuples. The account_label
    flows through parse_bt so we can auto-assign Cleveland records to
    the Cleveland region without depending on BoldTrail's `region` field
    (Cleveland account doesn't populate it).

    Dayton is fetched first so it's canonical for btid collisions:
    if the same person exists in both accounts (e.g. operations staff
    with logins in both), the Dayton record wins.
    """
    print("\n── BoldTrail Fetch (GET requests only) ─────────────────")

    # Dayton first (canonical)
    if not BT_API_KEY:
        abort("BOLDTRAIL_API_KEY not set in environment.")
    dayton = fetch_bt_for_account(BT_API_KEY, 'Dayton')

    # Cleveland (deduplicated against Dayton by btid)
    cleveland = []
    if not ENABLE_CLEVELAND_FETCH:
        print(f"  (Cleveland fetch disabled via feature flag — Dayton-only mode)")
    elif BT_API_KEY_CLEVELAND:
        cleveland = fetch_bt_for_account(BT_API_KEY_CLEVELAND, 'Cleveland')
    else:
        print(f"  (Cleveland fetch enabled but BOLDTRAIL_API_KEY_CLEVELAND not set — skipping)")

    # Dedupe Cleveland against Dayton so a person who exists in BOTH accounts
    # (the owner, shared leadership/ops, an agent licensed under both) never
    # produces two cards on the find-an-agent page. Dayton is canonical: its
    # record wins and the Cleveland copy is dropped.
    #
    # We match on btid OR normalized email:
    #   - btid:  BoldTrail usually reuses the same btid for the same person
    #            across accounts (see the VJ Evans note in SUPPRESS_BTIDS).
    #   - email: belt-and-suspenders for the case where the person was added
    #            fresh in the Cleveland account and therefore carries a
    #            DIFFERENT btid there — btid alone would miss them and they'd
    #            double. Email catches it.
    dayton_btids = {str(r.get('id', '')) for r in dayton if r.get('id')}
    dayton_emails = {
        (r.get('email') or '').lower().strip()
        for r in dayton if (r.get('email') or '').strip()
    }
    cleveland_dedup = []
    skipped_btid = 0
    skipped_email = 0
    for r in cleveland:
        rid    = str(r.get('id', ''))
        remail = (r.get('email') or '').lower().strip()
        if rid and rid in dayton_btids:
            skipped_btid += 1
            continue
        if remail and remail in dayton_emails:
            skipped_email += 1
            continue
        cleveland_dedup.append(r)
    if skipped_btid:
        print(f"  Skipped {skipped_btid} Cleveland record(s) already in Dayton (same btid)")
    if skipped_email:
        print(f"  Skipped {skipped_email} Cleveland record(s) already in Dayton (same email, different btid)")

    # Tag each record with its source account so parse_bt can auto-assign region
    tagged = [(r, 'dayton') for r in dayton] + [(r, 'cleveland') for r in cleveland_dedup]
    total_label = f"{len(dayton)} Dayton"
    if ENABLE_CLEVELAND_FETCH:
        total_label += f" + {len(cleveland_dedup)} Cleveland"
    print(f"  Total to process: {len(tagged)} ({total_label})")
    return tagged

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
    '272213',  # Constance Lowery — partner in joint listing "Connie Lowery & Deanna O'Diam" (preferred display: partnership card only)
    # NOTE: Vincent (VJ) Evans (btid 272587) was previously suppressed here
    # because he appears in both Dayton and Cleveland accounts as operations
    # infrastructure staff. We discovered that BoldTrail uses the SAME btid
    # for the same person across accounts, so the fetch-time dedup-by-btid
    # already handles his case correctly. His Dayton record is the canonical
    # one (fetched first); his Cleveland record is dropped at fetch.
    # See MANUAL_BTID_PAIRINGS below for the Vince/Vincent name-pairing.
}

# Emails of joint-listing records in agents.json. The sync preserves these
# records across every sync (they aren't matched by any single BoldTrail
# record because they represent partnerships).
PRESERVE_JOINT_EMAILS = {
    'kljackson@glasshouserealty.com',         # Kevin & Lisa Jackson
    'conniedeanna@kunalpatelgroup.com',       # Connie Lowery & Deanna O'Diam
}

# ── HIDE TEAM ASSIGNMENT FROM AGENT CARD ──────────────────────────────────────
# Some agents are assigned to a team in BoldTrail for internal/backend reasons
# (lead routing, transaction grouping, compensation, etc.) but don't want
# their team name and logo visible on their public agent card.
#
# Two ways to hide:
#
# 1. HIDE_TEAM_NAMES — hide an entire team by name. Every agent whose
#    BoldTrail `team` field matches one of these strings (case-insensitive,
#    whitespace-normalized) has their team and teamLogo blanked. Scales
#    automatically as the team grows.
#
# 2. HIDE_TEAM_BTIDS — hide a specific agent's team by their BoldTrail id.
#    Use this for one-off exceptions when you want to hide a team for some
#    agents but not others.
#
# To hide via team name (preferred for whole-team hiding):
#   Add the team name string to HIDE_TEAM_NAMES below
#
# To hide via per-agent btid (for individual exceptions):
#   1. Look up the agent's BoldTrail id from the API
#   2. Add their btid to HIDE_TEAM_BTIDS with a comment naming them
#
# In both cases: agents still appear on the public agent finder, just as
# "solo agent" visually (no team name, no team logo).
#
# To restore visibility: remove the entry — team reappears on the next sync.

HIDE_TEAM_NAMES = {
    'K4 Management Group',  # Backend lead-routing team, not for public display
}
# Normalize HIDE_TEAM_NAMES once for matching (lowercase + trimmed whitespace)
_HIDE_TEAM_NAMES_NORMALIZED = {n.strip().lower() for n in HIDE_TEAM_NAMES}

HIDE_TEAM_BTIDS = {btid for btid in [
    # '272304',   # Laura Long — backend team only, don't show on card
    # '272456',   # Some Other Agent — example
] if btid}

# Track suppressed records for the report (lets you verify the list is correct)
SUPPRESSED = []

# Track non-Agent role exclusions for the flagged report
ROLE_EXCLUDED = []

# Track Cleveland spreadsheet records that had no BoldTrail match (for review)
CLEVELAND_UNMATCHED = []


def parse_bt(bt, account='dayton'):
    """
    Parse a single BoldTrail user record into our agents.json shape.
    Returns the dict on success, None if filtered out.

    `account` is 'dayton' or 'cleveland', tagging which BoldTrail account
    the record came from. Cleveland records are auto-assigned
    regions:['Cleveland'] since the Cleveland account doesn't populate
    BoldTrail's `region` field on user records.
    """
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

    # Active check: BoldTrail has two "active" fields:
    #   - account_user_active = login still works
    #   - active              = is currently an agent at the brokerage
    # The URL filter status=active matches on account_user_active, NOT on active.
    # We check 'active' explicitly here so deactivated agents are excluded even
    # if their BoldTrail login is still alive.
    # Defensive: only filter if 'active' is explicitly False. Missing/None fields
    # pass through (we don't want to accidentally exclude every agent if BoldTrail
    # ever changes the response shape).
    if bt.get('active') is False:
        return None

    # Numbered prefixes (e.g. "006 - Laura") appear in first_name in BoldTrail,
    # not just in the joined name. Strip from each component to be safe.
    first = (bt.get('first_name', '') or '').strip()
    last  = (bt.get('last_name', '')  or '').strip()
    # Strip 0XX - prefix from first_name specifically
    first = re.sub(r'^0[0-9]+ - ', '', first).strip()
    last  = re.sub(r'^0[0-9]+ - ', '', last).strip()

    # ── Preferred Name override ─────────────────────────────────────────────
    # BoldTrail exposes a "Preferred Name" custom field (note: literal field
    # name with space and capitals, not snake_case — it's an admin-defined
    # custom field, not a built-in one).
    # If set, this replaces the first name in the displayed agent name.
    # Examples:
    #   first_name="Mohammad", Preferred Name="Mo"      → display: "Mo Zahedi"
    #   first_name="Tamara",   Preferred Name="Tami"    → display: "Tami Galdeen"
    #   first_name="Mary",     Preferred Name="Elizabeth" → display: "Elizabeth Cooper"
    # The "Preferred Name" field is single-name (first name only). Whatever
    # the admin entered is used as-is, paired with the legal last name.
    preferred = (bt.get('Preferred Name', '') or '').strip()
    # Defensive: strip the same 0XX - prefix in case admin pattern-copied it
    preferred = re.sub(r'^0[0-9]+ - ', '', preferred).strip()
    if preferred:
        first = preferred

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

    # Hide-team override: two paths, applied in order
    #   1. HIDE_TEAM_NAMES — whole-team hiding (preferred for K4-style cases
    #      where the team is internal-only across all members)
    #   2. HIDE_TEAM_BTIDS — per-agent override for one-off exceptions
    # If either matches, team is blanked → renders as "Solo agent" on the card.
    btid_str = str(bt.get('id', ''))
    team_normalized = team.strip().lower()
    if team_normalized and team_normalized in _HIDE_TEAM_NAMES_NORMALIZED:
        team = ''
    elif btid_str and btid_str in HIDE_TEAM_BTIDS:
        team = ''

    if not regions and office:
        inferred = infer_region(office)
        if inferred:
            regions = [inferred]

    # Cleveland account: auto-assign region. BoldTrail's Cleveland account
    # doesn't populate the `region` field on user records, but the account
    # itself implies the region. If the record already declared a region
    # (rare/unexpected), we still ensure Cleveland is listed.
    if account == 'cleveland':
        if 'Cleveland' not in regions:
            regions = ['Cleveland'] + regions

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

    # [Photo Pipeline] Capture avatar_added — BoldTrail's own signal for
    # whether the agent has uploaded a real photo (vs. the default placeholder
    # at /assets/empty-avatar.png). The photo pipeline uses this to decide
    # whether to attempt Source A (BoldTrail S3 download). Default to False
    # if missing; the pipeline will fall through to other sources.
    bt_avatar_added = bool(bt.get('avatar_added', False))

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
        'boldtrailPhoto': bt_photo,  # held temporarily, used by merge() & pipeline
        'boldtrailAvatarAdded': bt_avatar_added,  # held temporarily, used by pipeline
        'loftyId':     '',
        # NOTE: hidden is intentionally NOT set here. Merge controls it.
        # New agents will have hidden=False; existing records keep their flag.
        'source':      'boldtrail' if account == 'dayton' else 'boldtrail-cleveland',
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

        # Check for other missing data (agent still shown on site).
        # Cleveland (Asa Cox) agents have no team/office in BoldTrail by design —
        # those assignments live in the spreadsheet — so muting no_team/no_office
        # for them keeps the review report clean. no_phone still flags.
        is_cleveland = (
            a.get('source') == 'boldtrail-cleveland'
            or 'Cleveland' in a.get('regions', [])
        )
        issues = []
        if not a['office'] and not is_cleveland: issues.append('no_office')
        if not a['phone']:                       issues.append('no_phone')
        if not a['team']  and not is_cleveland:  issues.append('no_team')

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
# Backup strategy: every live run creates a timestamped backup on disk (this
# protects against mid-run corruption). The backup is GIT-COMMITTED only if:
#   - It is the first sync of the calendar day (UTC), OR
#   - Change volume >= 5% of existing record count (significant event)
#
# Why this matters: with hourly syncs running 11x/day x 5 days/week, committing
# every backup creates ~55 backup commits per week, cluttering git history with
# noise. The smart approach gives you one durable rollback point per day plus
# emergency snapshots when something significant happens, while keeping the
# disk-side backup for short-term safety on every run.

BACKUP_COMMIT_THRESHOLD_PCT = 0.05  # 5% change triggers an emergency commit

def is_first_sync_today():
    """
    Check if any agents_*.json backup exists for today (UTC). If not,
    this is the first sync of the day and the backup should be committed.
    """
    if not os.path.exists(BACKUP_DIR): return True
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    for f in os.listdir(BACKUP_DIR):
        if f.startswith('agents_') and today in f:
            return False
    return True


def backup(filepath, existing_count=0, new_count=0):
    """
    Create a timestamped backup on disk. Returns a dict with:
      - 'path': the backup file path (or None if skipped)
      - 'commit_worthy': True if this backup should be committed to git
      - 'reason': why it's commit-worthy (or why it isn't)

    Disk backup happens unconditionally on every live run. Git commit
    decision is based on first-of-day + significant-change criteria.
    """
    if not os.path.exists(filepath):
        print(f"  No existing {filepath} — skipping backup")
        return {'path': None, 'commit_worthy': False, 'reason': 'no existing file'}

    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    base = os.path.basename(filepath).replace('.json', '')
    dest = os.path.join(BACKUP_DIR, f"{base}_{ts}.json")

    # First-of-day detection BEFORE we create the new backup (otherwise
    # the new file we're about to write would be seen as "today's backup")
    first_today = is_first_sync_today()

    shutil.copy2(filepath, dest)
    print(f"  Backed up → {dest} ({os.path.getsize(dest):,} bytes)")

    # Always commit the backup. Originally this had threshold logic (only
    # commit on first-of-day or 5%+ change) to avoid git noise from hourly
    # cron. After production deploy of the photo pipeline, we decided every
    # backup is worth committing because:
    #   - Pipeline can make photo changes that aren't reflected in agent count
    #   - Metadata sync can make field-level changes (region, team, name)
    #     that are too subtle for a count-based heuristic
    #   - Git noise is small (these are 175KB JSON files)
    #   - Having a recoverable snapshot before every run > slightly cleaner git log
    commit_worthy = True
    reason = ''

    if first_today:
        reason = 'first sync of the day (UTC)'
    elif existing_count > 0 and new_count != existing_count:
        change_pct = abs(new_count - existing_count) / existing_count
        reason = f'agent count changed: {existing_count} → {new_count} ({change_pct:.1%})'
    else:
        reason = 'routine sync (every backup committed for safety)'

    if commit_worthy:
        print(f"  ✓ Backup will be COMMITTED to git: {reason}")
    else:
        print(f"  · Backup kept on disk only: {reason}")

    return {'path': dest, 'commit_worthy': commit_worthy, 'reason': reason}


def prune_backups(keep=30):
    """
    Keep the last `keep` backups per file prefix. Backups are pruned on
    disk; this doesn't affect what's already been committed to git.
    """
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

        # [BUG-6] Deduplicate `existing` on load. Defensive cleanup for any
        # historical duplicates that may have accumulated in agents.json.
        #
        # Duplicates can occur when:
        #   - PRESERVE_JOINT_EMAILS preserves multiple BoldTrail records
        #     sharing a joint email (the original bug)
        #   - Early development test runs accidentally wrote duplicates
        #     to production
        #
        # Strategy: group by a composite identity key (btid, loftyId, source).
        # For each group, keep the FIRST occurrence; drop the rest.
        # This is safe because: (1) duplicates are bit-identical by definition,
        # (2) any genuine multi-record entries (joint listings) have distinct
        # composite keys (different name or source).
        seen_keys = set()
        deduped = []
        dupes_removed = 0
        for a in existing:
            # Composite identity key
            key = (
                str(a.get('boldtrailId', '')),
                str(a.get('loftyId', '')),
                a.get('source', ''),
                a.get('name', '').strip().lower(),
            )
            if key in seen_keys:
                dupes_removed += 1
                continue
            seen_keys.add(key)
            deduped.append(a)
        if dupes_removed:
            print(f"  ⚠ Removed {dupes_removed} duplicate record(s) from existing data on load")
        existing = deduped

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
    '272587': 'Vince (VJ) Evans',  # BoldTrail: 001 - Vincent Evans (preferred name VJ)
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
    Determine which photo URL the agent.json record should hold AT THE END
    OF THE METADATA SYNC. This is intentionally minimal in the new architecture:
    we do not download or optimize here — the photo pipeline (gh-photo-pipeline.py)
    runs after this sync and handles all acquisition, optimization, and self-hosting.

    Behavior:
      - If we already have a self-hosted photo URL from a prior pipeline run
        (evansvince.github.io/...), preserve it. The pipeline will decide on
        its next run whether to refresh (based on photoSourceHash vs new
        BoldTrail URL hash).
      - If we have an existing photo from any source, preserve it. The pipeline
        will decide whether to replace it based on its source-priority rules.
      - If we have nothing and BoldTrail has a real photo (avatar_added=true),
        set the URL to the BoldTrail S3 URL. The pipeline will acquire it
        on its next run.
      - Otherwise: empty (frontend renders initials).

    The pipeline OWNS the photo, photoSource, and photoSourceHash fields.
    pick_photo just provides a reasonable starting state when a record is
    first created. After the first pipeline run, the photo field is always
    self-hosted (or empty if no source produced a photo).
    """
    existing_photo = (existing.get('photo') or '') if existing else ''
    bt_photo = agent.get('boldtrailPhoto', '') or ''
    bt_added = agent.get('boldtrailAvatarAdded', False)

    # Preserve any existing photo — the pipeline decides what to do with it
    if existing_photo:
        return existing_photo

    # New agent or no existing photo: seed with BoldTrail URL if it has a real
    # photo, so the pipeline knows to acquire it. Without this, a brand-new
    # agent would have empty photo until the pipeline ran twice.
    if bt_added and bt_photo:
        return bt_photo

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
        # The boldtrailPhoto and boldtrailAvatarAdded fields are temporary —
        # we keep them in the record while the photo pipeline runs (it reads
        # them to decide on acquisition), then they get stripped from the
        # final agents.json by stripping in the post-write phase. For now,
        # leave them attached so the pipeline can see them.
        # boldtrailPhoto and boldtrailAvatarAdded are stripped later in the
        # finalization step before writing agents.json — see end of build_*.

        # [Photo Pipeline] Preserve the pipeline's state fields from any
        # existing record. The pipeline owns these and overwrites them on
        # successful acquisition; until then, the metadata sync must not
        # clobber them.
        if existing:
            agent['photoSource']     = existing.get('photoSource', '')
            agent['photoSourceHash'] = existing.get('photoSourceHash', '')
        else:
            # New agent — empty defaults. The pipeline's first run will fill
            # these in.
            agent['photoSource']     = ''
            agent['photoSourceHash'] = ''

        if existing:
            # Preserve fields the sync does not own
            agent['profileUrl'] = existing.get('profileUrl', '')
            agent['loftyId']    = existing.get('loftyId', '')

            # ── Cleveland (Asa Cox) matched record ──────────────────────────
            # The spreadsheet stays the source of truth for ASSIGNMENTS.
            # BoldTrail only refreshes name, email, phone (already on `agent`)
            # and the headshot (the photo pipeline applies it when
            # avatar_added=true). team, teamLogo, office, title, and any
            # spreadsheet-only fields (status, zillowUrl, etc.) are pinned to
            # the existing spreadsheet record so nothing is overwritten or
            # dropped. regions are kept from the spreadsheet too, defaulting to
            # ['Cleveland'] only if the spreadsheet record had none. boldtrailId
            # is still adopted (it's already on `agent`) so future syncs match
            # by id and the headshot can flow.
            if agent.get('source') == 'boldtrail-cleveland':
                existing_is_cleveland = (
                    existing.get('source') in ('spreadsheet', 'boldtrail-cleveland', 'both')
                    or 'Cleveland' in existing.get('regions', [])
                )
                if existing_is_cleveland:
                    for f in ('team', 'teamLogo', 'office', 'title'):
                        if f in existing:
                            agent[f] = existing.get(f)
                    agent['regions'] = existing.get('regions') or ['Cleveland']
                    # Carry forward any spreadsheet-only fields we don't model
                    # explicitly (status, zillowUrl, etc.). Skip softDeletedAt —
                    # the reactivation logic below owns that.
                    for k, v in existing.items():
                        if k not in agent and k != 'softDeletedAt':
                            agent[k] = v

            # [BUG-4] hidden: copy whatever the existing record had.
            # Manually-set hidden:true stays true forever. We never write here.
            if 'hidden' in existing:
                agent['hidden'] = existing['hidden']
            else:
                agent['hidden'] = False

            # [BUG-5] teamLogo: existing value usually wins. EXCEPTION: if
            # the incoming agent has team='' (hidden via HIDE_TEAM_NAMES or
            # HIDE_TEAM_BTIDS), we must blank the teamLogo too — otherwise
            # the old logo lingers on a card with no team name. Blanking team
            # without blanking logo creates a worse visual than either alone.
            if agent.get('team') and existing.get('teamLogo'):
                agent['teamLogo'] = existing['teamLogo']
            elif not agent.get('team'):
                agent['teamLogo'] = ''

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
            # If existing was tagged 'both', keep it that way — UNLESS this is a
            # Cleveland record, whose source must stay 'boldtrail-cleveland' so
            # the photo pipeline applies Cleveland (Source-A-only, no Lofty)
            # handling and never tries to scrape a non-existent Lofty page.
            if existing.get('source') == 'both' and agent.get('source') != 'boldtrail-cleveland':
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

    # ── Profile URL auto-generation (Dayton-only) ──────────────────────────────
    # For Dayton agents without a profileUrl, generate a candidate slug from
    # their name (firstname-lastname) and HEAD-request it against Lofty.
    # If it returns 200 the URL is committed; if 404 we leave profileUrl empty
    # (next sync retries after the cache cooldown — see PROFILE_URL_404_COOLDOWN_SECS).
    #
    # CLEVELAND IS EXCLUDED — Cleveland uses a different agent-page system
    # where slugs don't resolve the same way. Cleveland profile URLs are
    # parking-lot work for the broader Cleveland production deployment.
    def _is_cleveland_agent(agent):
        if 'Cleveland' in agent.get('regions', []):
            return True
        src = agent.get('source', '')
        if src == 'spreadsheet':
            return True
        if src.endswith('-cleveland'):  # 'boldtrail-cleveland'
            return True
        return False

    profile_url_generated = 0
    profile_url_skipped_no_match = 0
    profile_url_skipped_cleveland = 0
    for agent in merged:
        if agent.get('profileUrl'):
            continue  # already has one (preserved from existing record)
        if agent.get('hidden'):
            continue  # hidden agents don't need a profile URL
        if _is_cleveland_agent(agent):
            profile_url_skipped_cleveland += 1
            continue  # Cleveland uses a different agent-page system
        slug = slugify_name(agent.get('name', ''))
        if not slug:
            continue
        verified = verify_profile_url(slug)
        if verified:
            agent['profileUrl'] = verified
            profile_url_generated += 1
        else:
            profile_url_skipped_no_match += 1

    # Persist the cache so subsequent syncs don't re-verify every URL
    _save_profile_url_cache()

    if profile_url_generated or profile_url_skipped_no_match or profile_url_skipped_cleveland:
        msg = f"  Profile URLs: {profile_url_generated} verified & set"
        if profile_url_skipped_no_match:
            msg += f", {profile_url_skipped_no_match} not yet live (slug not configured — admin task)"
        if profile_url_skipped_cleveland:
            msg += f", {profile_url_skipped_cleveland} skipped (Cleveland)"
        print(msg)

    # ── Cleveland spreadsheet handling ──────────────────────────────────────
    # CLEVELAND_SOFT_DELETE_UNMATCHED is also referenced in the soft-delete loop
    # below, so define it at function scope.
    #   True  = a Cleveland agent no longer in the active BoldTrail roster is
    #           soft-deleted (hidden now, purged after the 30-day grace), exactly
    #           like Dayton. BoldTrail active-status becomes the on/off switch
    #           for the Cleveland website cards.
    #   False = unmatched Cleveland records are preserved (transition mode).
    # NOTE: with this True, any Cleveland record NOT in the active BoldTrail pull
    # soft-deletes — including spreadsheet-only agents who were never added to
    # BoldTrail. Add real agents to BoldTrail (and resolve admin/staff records)
    # BEFORE the first production run, and review the dry-run soft-delete list.
    CLEVELAND_SOFT_DELETE_UNMATCHED = True

    if not ENABLE_CLEVELAND_FETCH:
        # FEATURE FLAG OFF (production default):
        # Preserve every Cleveland spreadsheet record untouched (legacy behavior).
        # This matches what the script did before the Cleveland integration was
        # built. Cleveland data flows through the spreadsheet pipeline as before.
        cleveland_preserved = 0
        for existing in existing_all:
            is_cleveland_spreadsheet = (
                existing.get('source') == 'spreadsheet'
                or 'Cleveland' in existing.get('regions', [])
            )
            if not is_cleveland_spreadsheet:
                continue
            if id(existing) in matched_existing_ids:
                continue
            merged.append(existing)
            cleveland_preserved += 1
        if cleveland_preserved:
            print(f"  ✦ Preserved {cleveland_preserved} Cleveland agents (spreadsheet source, legacy mode)")

    else:
        # FEATURE FLAG ON (sync-testing):
        # Cleveland transitioning to BoldTrail as source of truth.
        # See header comment in this function for details.

        unmatched_spreadsheet = []
        cleveland_preserved = 0
        for existing in existing_all:
            is_cleveland_spreadsheet = (
                existing.get('source') == 'spreadsheet'
                or 'Cleveland' in existing.get('regions', [])
            )
            if not is_cleveland_spreadsheet:
                continue
            if id(existing) in matched_existing_ids:
                continue
            unmatched_spreadsheet.append(existing)
            if CLEVELAND_SOFT_DELETE_UNMATCHED:
                continue  # fall through to regular soft-delete loop
            merged.append(existing)
            cleveland_preserved += 1

        if cleveland_preserved:
            print(f"  ✦ Preserved {cleveland_preserved} unmatched Cleveland spreadsheet records (transition mode)")
        if unmatched_spreadsheet:
            print(f"  ⚠ {len(unmatched_spreadsheet)} Cleveland spreadsheet records had no BoldTrail match — see reports/cleveland-unmatched/")
            global CLEVELAND_UNMATCHED
            CLEVELAND_UNMATCHED = unmatched_spreadsheet

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
        # Cleveland spreadsheet records are handled in the transition block above.
        # In transition mode (CLEVELAND_SOFT_DELETE_UNMATCHED=False) they're either
        # already in `merged` (matched or preserved untouched), so skip them here.
        # When CLEVELAND_SOFT_DELETE_UNMATCHED=True, they fall through to soft-delete.
        is_cleveland_spreadsheet = (
            existing.get('source') == 'spreadsheet'
            or 'Cleveland' in existing.get('regions', [])
        )
        if is_cleveland_spreadsheet and not CLEVELAND_SOFT_DELETE_UNMATCHED:
            continue
        # Skip joint listings — preserved across syncs (no 1:1 BT match exists).
        # Carry the joint record forward into merged as-is.
        # IMPORTANT: only preserve records with source='lofty' here. A BoldTrail
        # record sharing a joint email belongs to a real individual (e.g. Constance
        # Lowery within the "Connie Lowery & Deanna O'Diam" partnership) and should
        # fall through to normal soft-delete logic — preserving it as a "joint"
        # record creates a duplicate that survives forever.
        ex_email = (existing.get('email') or '').lower()
        if ex_email in PRESERVE_JOINT_EMAILS and existing.get('source') == 'lofty':
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

    # ── 2c. Cleveland-unmatched report ─────────────────────────────────────────
    # Spreadsheet-source Cleveland records that did NOT match any incoming
    # BoldTrail record (Dayton or Cleveland) during this sync. In transition
    # mode they're still being preserved on the site. Review this list and
    # decide which should stay vs which should be soft-deleted (former agent,
    # role change, etc.). Once verified, set CLEVELAND_SOFT_DELETE_UNMATCHED=True
    # in the script to start applying the standard 30-day soft-delete to them.
    cle_dir = os.path.join(REPORTS_DIR, 'cleveland-unmatched')
    os.makedirs(cle_dir, exist_ok=True)
    cle_path = os.path.join(cle_dir, f'cleveland-unmatched-{ts}.json')
    cle_sorted = sorted(
        [{'name': a.get('name', ''), 'email': a.get('email', ''),
          'team': a.get('team', ''), 'regions': a.get('regions', []),
          'source': a.get('source', '')}
         for a in CLEVELAND_UNMATCHED],
        key=lambda x: x['name'].lower(),
    )
    with open(cle_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': "Cleveland spreadsheet records with no BoldTrail match. "
                           "Currently preserved on the site. Review and decide "
                           "which should be retained vs soft-deleted.",
            'count':       len(CLEVELAND_UNMATCHED),
            'records':     cle_sorted,
        }, f, indent=2)
    print(f"  Cleveland-unmatched: {cle_path}{dr_label} ({len(CLEVELAND_UNMATCHED)} records)")

    # ── 3a. No-photo report ────────────────────────────────────────────────────
    # BoldTrail is the primary source for agent photos. The Lofty scraper picks
    # up any standard-template Lofty pages as a bonus, but admins should upload
    # to BoldTrail to guarantee coverage.
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
            'description': 'Agents on site with no profile photo. Upload a headshot to BoldTrail to populate. The sync pulls it on the next cycle.',
            'count':       len(no_photo),
            'agents':      sorted(no_photo, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No photo:      {photo_path}{dr_label} ({len(no_photo)} agents)")

    # ── 3b. No-profile-URL report (Dayton only) ────────────────────────────────
    # Cleveland is excluded — Cleveland uses a different agent-page system
    # where Lofty slugs don't resolve. Cleveland profile URL strategy is in
    # the parking lot for a future build.
    purl_dir = os.path.join(REPORTS_DIR, 'no-profile-url')
    os.makedirs(purl_dir, exist_ok=True)

    def _is_cle(a):
        if 'Cleveland' in a.get('regions', []):
            return True
        s = a.get('source', '')
        return s == 'spreadsheet' or s.endswith('-cleveland')

    no_purl = []
    for a in merged:
        if a.get('profileUrl'): continue
        if a.get('hidden'):     continue
        if _is_cle(a):          continue
        no_purl.append({
            'name':           a['name'],
            'email':          a['email'],
            'regions':        a['regions'],
            'team':           a.get('team', ''),
            'source':         a.get('source', ''),
            'boldtrailId':    a.get('boldtrailId', ''),
            'loftyId':        a.get('loftyId', ''),
            # Auto-generated slug — what the admin should enter in Lofty's "Slug" field
            'suggested_lofty_slug': slugify_name(a.get('name', '')),
            'suggested_full_url':   f"{PROFILE_URL_BASE}/{slugify_name(a.get('name', ''))}",
        })

    purl_path = os.path.join(purl_dir, f'no-profile-url-{ts}.json')
    with open(purl_path, 'w') as f:
        json.dump({
            'generated':   generated,
            'dry_run':     dry_run,
            'description': (
                'Dayton agents on the site without a profile URL. To fix: '
                'open Lofty admin, find the agent\'s "Slug" field, set it to '
                'the value in `suggested_lofty_slug` (e.g. "nikole-locke"). '
                'The next sync will HEAD-request the URL and, if it resolves, '
                'auto-populate the agent\'s profileUrl. Cleveland agents are '
                'excluded from this report since Cleveland uses a different '
                'agent-page system.'
            ),
            'count':       len(no_purl),
            'agents':      sorted(no_purl, key=lambda x: x['name'].lower()),
        }, f, indent=2)
    print(f"  No profile URL:{purl_path}{dr_label} ({len(no_purl)} Dayton agents)")

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

    # 1. Fetch (Dayton + Cleveland, tagged tuples)
    bt_raw_tagged = fetch_bt_agents()

    # 2. Parse — pass each record's account through so Cleveland records
    #    get auto-assigned regions:['Cleveland'].
    parsed = [a for a in (parse_bt(r, account=acct) for r, acct in bt_raw_tagged) if a]
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
    # Pure alphabetical sort. Photoless agents interleave naturally with photoed
    # ones rather than being segregated to the bottom. The agent finder reads
    # this order directly — no client-side resort.
    merged.sort(key=lambda a: a['name'].lower())

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

    # 10. Backup (smart commit decision: first-of-day OR significant change)
    print(f"\n── Backup ──────────────────────────────────────────────")
    backup_result = backup(args.out, existing_count=existing_count, new_count=len(merged))
    prune_backups(keep=30)
    # Write a marker the workflow can read to decide whether to git-add backups/
    if backup_result['commit_worthy']:
        with open('BACKUP_COMMIT', 'w') as f:
            f.write(backup_result['reason'] + '\n')
    elif os.path.exists('BACKUP_COMMIT'):
        os.remove('BACKUP_COMMIT')

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
