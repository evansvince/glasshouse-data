#!/usr/bin/env python3
"""
Glasshouse Lofty Photo Scraper
═══════════════════════════════════════════════════════════════════════════

Pulls agent headshots from Lofty-hosted profile pages for agents who have
a profile URL set but no photo in agents.json.

Why we need this:
  - Lofty's API doesn't expose user/agent records (confirmed by Lofty support)
  - But Lofty serves a public agent profile page at the profileUrl we already
    have for each agent (set via Lofty slug + our auto-verification)
  - The agent's headshot is rendered server-side into the page HTML
  - So: simple HTTP GET + HTML parse pulls the photo URL, no auth needed

What this does:
  - Loads agents.json
  - Selects agents matching ALL of:
      * profileUrl is set (non-empty)
      * photo is empty (no headshot yet)
      * source != 'boldtrail-cleveland' (Dayton-only — Cleveland uses a
        different profile system)
      * not hidden, not soft-deleted
  - For each, GETs the profileUrl, follows redirects to wherever Lofty
    serves the page, parses out the <img> inside <div class="agent-card">
  - Validates the alt attribute matches the agent name (to confirm we got
    the right photo, not a banner/decoration image)
  - Saves the cdn.lofty.com photo URL into the agent's photo field
  - Caches the (profileUrl -> photoUrl) mapping so we don't re-scrape
    unchanged pages on every run

What this does NOT do:
  - Does not write to Lofty, BoldTrail, or any external system
  - Does not download/optimize the photo (Lofty's CDN already serves an
    optimized w640 .webp; the photo optimizer is only for BoldTrail S3
    raw uploads)
  - Does not modify agents.json directly during dry-run mode

Safety posture:
  - Strictly READ-ONLY HTTP GETs
  - Polite User-Agent identifying our brokerage
  - 1-second delay between requests to be a good neighbor
  - Per-request timeout to prevent hangs
  - Cache to avoid re-scraping unchanged pages
  - Logs every attempted scrape for the report

Usage:
  python3 gh-lofty-scraper.py              # process pending; modifies agents.json
  python3 gh-lofty-scraper.py --dry-run    # preview, no changes
  python3 gh-lofty-scraper.py --limit 10   # cap how many agents to process this run
  python3 gh-lofty-scraper.py --force <name>  # re-scrape a specific agent even if they have a photo

Exits 0 always (failures are logged, not fatal — main sync should never
be blocked by scraper issues).
"""

import argparse
import html as html_module
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
AGENTS_FILE = 'agents.json'
CACHE_FILE  = 'lofty-scrape-cache.json'
REPORT_DIR  = 'reports/lofty-scraper'

# HTTP settings
USER_AGENT       = 'GlasshouseRealty-AgentSync/1.0 (+contact: ops@glasshouserealty.com)'
REQUEST_TIMEOUT  = 20    # seconds per page
DELAY_BETWEEN    = 1.0   # seconds between requests (politeness)

# Photo URL patterns: Lofty CDN photos look like
#   https://cdn.lofty.com/image/fs/web/.../w640_original_*.webp
# Legacy photos still served from Chime's old CDN (Lofty was rebranded from Chime):
#   https://cdn.chime.me/image/fs/user-info/...
# We accept either. Both are first-party Lofty/Chime infrastructure.
LOFTY_CDN_HOSTS = ('cdn.lofty.com', 'cdn.chime.me')

# Cache TTL: even if a page hasn't changed, re-check periodically in case
# the agent updates their photo in Lofty admin.
CACHE_TTL_DAYS = 7


# ── CACHE ────────────────────────────────────────────────────────────────────
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def cache_is_fresh(cache_entry):
    """Return True if cache entry exists and is within TTL."""
    if not cache_entry:
        return False
    ts = cache_entry.get('scrapedAt')
    if not ts:
        return False
    try:
        cached_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        age_days = (datetime.now(timezone.utc) - cached_dt).total_seconds() / 86400
        return age_days < CACHE_TTL_DAYS
    except (ValueError, TypeError):
        return False


# ── HTTP ─────────────────────────────────────────────────────────────────────
def fetch_html(url):
    """
    GET the agent profile page and return its HTML body as a string.
    Follows redirects (urllib does this by default with HTTPRedirectHandler).
    Returns None on failure.
    """
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent':      USER_AGENT,
            'Accept':          'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
            # Try utf-8 first, fall back to latin-1 which never fails to decode
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('latin-1')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return None


# ── PARSING ──────────────────────────────────────────────────────────────────
# Regex that captures the <img ... src="..." alt="..."> inside the agent-card.
# We anchor on <div class="agent-card"> and look for the FIRST <img> within it
# that has both src and alt. Vue 3 comment placeholders (<!---->) are fine.
#
# The pattern is intentionally loose around whitespace and attribute order
# since browsers and Vue can reorder/reformat attributes in ways we can't
# predict perfectly. We require:
#   1. agent-card div opens somewhere before
#   2. an <img> tag with src and alt
#   3. src starts with https:// (defensive)
#
# We extract the FIRST matching <img> inside the agent-card. If Lofty ever
# adds multiple <img> elements per card (background + headshot), we'd need
# to refine. For now the first <img> is the headshot.
AGENT_CARD_OPEN = re.compile(r'<div\s+class="agent-card"', re.IGNORECASE)
IMG_TAG_PATTERN = re.compile(
    r'<img\b[^>]*?\bsrc="(https://[^"]+)"[^>]*?\balt="([^"]*)"',
    re.IGNORECASE | re.DOTALL,
)
# Defensive fallback if attribute order is reversed (alt before src)
IMG_TAG_PATTERN_ALT_FIRST = re.compile(
    r'<img\b[^>]*?\balt="([^"]*)"[^>]*?\bsrc="(https://[^"]+)"',
    re.IGNORECASE | re.DOTALL,
)


def extract_photo_url(html, expected_name):
    """
    Parse the page HTML for the agent's headshot.
    Returns (photo_url, reason) where reason is None on success.
    """
    if not html:
        return None, 'empty_html'

    # Locate the agent-card section
    m = AGENT_CARD_OPEN.search(html)
    if not m:
        return None, 'no_agent_card_div'

    # Look at the HTML from the agent-card opening onward
    card_html = html[m.start():]

    # Try src-first attribute order
    img_match = IMG_TAG_PATTERN.search(card_html)
    if img_match:
        src, alt = img_match.group(1), img_match.group(2)
    else:
        # Try alt-first attribute order
        img_match = IMG_TAG_PATTERN_ALT_FIRST.search(card_html)
        if img_match:
            alt, src = img_match.group(1), img_match.group(2)
        else:
            return None, 'no_img_in_agent_card'

    # Decode HTML entities in alt (Lofty might encode &amp;, &#39;, etc.)
    alt = html_module.unescape(alt or '').strip()

    # Defensive: require the URL be from Lofty/Chime CDN. We don't want to grab
    # decorative placeholder images, social icons, etc. cdn.chime.me is Lofty's
    # legacy CDN from when they were called Chime; some agent photos still
    # serve from there.
    if not any(host in src for host in LOFTY_CDN_HOSTS):
        return None, f'non_lofty_image:{src[:60]}'

    # Validate the alt attribute roughly matches the agent name. This catches
    # cases where the agent-card div doesn't render or contains a different
    # agent's photo. We allow flexibility for nicknames, suffixes, etc.
    expected_lower = expected_name.lower().strip()
    alt_lower = alt.lower().strip()
    if alt_lower:
        # Strong match: alt == expected_name (case-insensitive)
        # Loose match: at least one of first/last name token is in alt
        if alt_lower == expected_lower:
            pass  # exact match, all good
        else:
            # Check whether at least one name token matches
            expected_tokens = set(expected_lower.split())
            alt_tokens = set(alt_lower.split())
            common = expected_tokens & alt_tokens
            if not common:
                return None, f'alt_name_mismatch:expected={expected_name},got={alt}'
            # Partial match is acceptable but flagged for review
    # If alt is empty, that's odd but not fatal — we still take the URL
    # because the agent-card scoping gives us high confidence

    return src, None


# ── ELIGIBILITY ──────────────────────────────────────────────────────────────
def is_eligible(agent):
    """
    Return (eligible: bool, reason: str). Eligible agents are scraping targets.
    """
    if agent.get('hidden'):
        return False, 'hidden'
    if agent.get('softDeletedAt'):
        return False, 'soft_deleted'
    if not agent.get('profileUrl'):
        return False, 'no_profile_url'
    if agent.get('photo'):
        return False, 'has_photo'
    src = agent.get('source', '')
    if src == 'boldtrail-cleveland':
        return False, 'cleveland_excluded'
    if src == 'cleveland-spreadsheet':
        return False, 'cleveland_excluded'
    return True, 'eligible'


# ── REPORT ───────────────────────────────────────────────────────────────────
def write_report(events, dry_run):
    """Write a JSON report of what the scraper did this run."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{REPORT_DIR}/lofty-scrape-{timestamp}.json'

    payload = {
        'generatedAt':    datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'mode':           'dry-run' if dry_run else 'LIVE',
        'totalAttempted': len(events),
        'successful':     sum(1 for e in events if e.get('status') == 'success'),
        'cached':         sum(1 for e in events if e.get('status') == 'cached'),
        'failed':         sum(1 for e in events if e.get('status') == 'failed'),
        'events':         events,
    }

    with open(filename, 'w') as f:
        json.dump(payload, f, indent=2)
    return filename


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Lofty agent photo scraper')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without writing agents.json')
    parser.add_argument('--limit', type=int, default=0,
                        help='Cap number of agents to process this run (0=no limit)')
    parser.add_argument('--force', metavar='NAME',
                        help='Re-scrape a specific agent by name even if they have a photo')
    parser.add_argument('--no-cache', action='store_true',
                        help='Ignore cache; re-scrape all eligible')
    parser.add_argument('--agents-file', default=AGENTS_FILE,
                        help=f'Path to agents JSON file (default: {AGENTS_FILE}). '
                             f'Use agents-test.json on the sync-testing branch.')
    args = parser.parse_args()

    # Resolve the agents file path (allows test env to use agents-test.json)
    agents_file = args.agents_file

    print('=' * 60)
    print('Glasshouse Lofty Photo Scraper')
    print(f'Timestamp:   {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
    print(f'Mode:        {"DRY-RUN" if args.dry_run else "LIVE"}')
    print(f'Agents file: {agents_file}')
    print('=' * 60)
    print('NOTE: HTTP GET only. No writes to Lofty.')
    print('=' * 60)

    # Load agents
    if not os.path.exists(agents_file):
        print(f'ERROR: {agents_file} not found')
        sys.exit(0)
    with open(agents_file) as f:
        agents = json.load(f)
    print(f'  Loaded {len(agents)} agents from {agents_file}')

    cache = load_cache()
    print(f'  Loaded {len(cache)} cached scrape results')

    # Identify eligible agents
    eligible = []
    skip_reasons = {}
    for a in agents:
        ok, reason = is_eligible(a)
        if args.force and a.get('name', '').lower() == args.force.lower():
            # Force-include this agent regardless of normal eligibility
            eligible.append(a)
            continue
        if ok:
            eligible.append(a)
        else:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    print(f'\n── Eligible Targets ──────────────────────────────────')
    print(f'  Eligible:           {len(eligible)} agents')
    if skip_reasons:
        print(f'  Skipped breakdown:')
        for reason, count in sorted(skip_reasons.items()):
            print(f'    {reason:30s} {count}')

    if not eligible:
        print('\n  No eligible agents to scrape. Done.')
        write_report([], args.dry_run)
        return

    # Apply --limit
    if args.limit > 0 and len(eligible) > args.limit:
        print(f'\n  --limit {args.limit} applied: processing {args.limit} of {len(eligible)}')
        eligible = eligible[:args.limit]

    # Process each
    print(f'\n── Scraping ──────────────────────────────────────────')
    events = []
    photos_updated = 0

    for i, agent in enumerate(eligible, 1):
        name = agent.get('name', 'Unknown')
        profile_url = agent.get('profileUrl', '')
        btid = agent.get('boldtrailId', '')

        event = {
            'name':       name,
            'profileUrl': profile_url,
            'btid':       btid,
            'attemptedAt': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        }

        # Cache check
        cache_key = profile_url
        cache_entry = cache.get(cache_key)
        if not args.no_cache and cache_is_fresh(cache_entry):
            cached_photo = cache_entry.get('photoUrl')
            if cached_photo:
                # Apply cached photo if agent still has no photo
                if not agent.get('photo'):
                    if not args.dry_run:
                        agent['photo'] = cached_photo
                        photos_updated += 1
                    event['status']  = 'cached'
                    event['photoUrl'] = cached_photo
                    print(f'  [{i}/{len(eligible)}] {name}: cached → applied')
                else:
                    event['status'] = 'cached_no_change'
                    print(f'  [{i}/{len(eligible)}] {name}: cached, already has photo')
            else:
                # Previously failed (cached as no-photo-found) and still in TTL
                event['status'] = 'cached_negative'
                event['reason'] = cache_entry.get('reason', 'cached_no_photo')
                print(f'  [{i}/{len(eligible)}] {name}: cached negative ({event["reason"]})')
            events.append(event)
            continue

        # Live fetch
        time.sleep(DELAY_BETWEEN)  # politeness
        html = fetch_html(profile_url)

        if html is None:
            event['status'] = 'failed'
            event['reason'] = 'fetch_failed'
            print(f'  [{i}/{len(eligible)}] {name}: FETCH FAILED')
            events.append(event)
            # Don't cache failures — retry next run
            continue

        photo_url, reason = extract_photo_url(html, name)

        if photo_url:
            # Got it
            event['status']   = 'success'
            event['photoUrl'] = photo_url
            if not args.dry_run:
                agent['photo'] = photo_url
                photos_updated += 1
            # Cache the success
            cache[cache_key] = {
                'photoUrl':  photo_url,
                'scrapedAt': event['attemptedAt'],
                'agentName': name,
            }
            print(f'  [{i}/{len(eligible)}] {name}: ✓ {photo_url[:80]}')
        else:
            # Couldn't find photo
            event['status'] = 'no_photo_found'
            event['reason'] = reason
            # Cache the negative result so we don't hammer the page next sync
            cache[cache_key] = {
                'photoUrl':  None,
                'scrapedAt': event['attemptedAt'],
                'agentName': name,
                'reason':    reason,
            }
            print(f'  [{i}/{len(eligible)}] {name}: ✗ {reason}')

        events.append(event)

    # Write outputs
    print(f'\n── Writing Outputs ──────────────────────────────────')
    if photos_updated > 0 and not args.dry_run:
        with open(agents_file, 'w') as f:
            json.dump(agents, f, indent=2)
        print(f'  ✓ Updated {agents_file} ({photos_updated} new photos)')
    else:
        print(f'  No {agents_file} update (dry-run or no photos found)')

    if not args.dry_run:
        save_cache(cache)
        print(f'  ✓ Cache saved ({len(cache)} entries)')

    report_path = write_report(events, args.dry_run)
    print(f'  ✓ Report: {report_path}')

    # Final summary
    successful = sum(1 for e in events if e.get('status') == 'success')
    cached_applied = sum(1 for e in events if e.get('status') == 'cached')
    failed = sum(1 for e in events if e.get('status') in ('failed', 'no_photo_found'))

    print(f'\n── Summary ──────────────────────────────────────────')
    print(f'  Attempted:         {len(eligible)}')
    print(f'  New photos found:  {successful}')
    print(f'  Cached applied:    {cached_applied}')
    print(f'  Failed/no photo:   {failed}')
    print(f'  Photos updated:    {photos_updated}')

    print('=' * 60)
    print('SCRAPER COMPLETE — No writes to Lofty.')
    print('=' * 60)


if __name__ == '__main__':
    main()
