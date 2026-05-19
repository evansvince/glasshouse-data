#!/usr/bin/env python3
"""
Glasshouse Photo Acquisition Pipeline
═══════════════════════════════════════════════════════════════════════════

The canonical photo handler. Replaces gh-photo-optimizer.py +
gh-lofty-scraper.py with a single unified pipeline.

PRINCIPLE
─────────
Every visible agent has exactly one photo URL on the website, pointing at
a file we host. The site never serves third-party CDN URLs directly.

PRIORITY CASCADE (per agent, every sync)
────────────────────────────────────────
1. Up-to-date check: self-hosted file exists AND BoldTrail URL hash matches.
2. Source A — BoldTrail S3 (avatar_added=true): canonical, wins over Lofty.
3. Source B — Lofty page scrape: bootstrap fallback.
4. Source C — keep existing self-hosted photo.
5. Source D — clear external URL → initials placeholder.

The pipeline OWNS photo, photoSource, and photoSourceHash on each record.
"""

import argparse
import hashlib
import html as html_module
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    from PIL import Image, ImageOps
except ImportError:
    print("ERROR: Pillow not installed. Install with: pip install Pillow")
    sys.exit(1)


# ── CONFIG ───────────────────────────────────────────────────────────────────
AGENTS_FILE      = 'agents.json'
PHOTO_DIR        = 'agent-photos'
STATE_FILE       = 'photo-pipeline-state.json'
ORPHAN_MARKER    = '.last-orphan-cleanup'
REPORT_DIR       = 'reports/photo-pipeline'

MAX_DIMENSION    = 800
TARGET_KB        = 150

PHOTO_PUBLIC_BASE = 'https://evansvince.github.io/glasshouse-data/agent-photos'
LOFTY_CDN_HOSTS   = ('cdn.lofty.com', 'cdn.chime.me')

DOWNLOAD_TIMEOUT = 30
PAGE_TIMEOUT     = 20
USER_AGENT       = 'GlasshouseRealty-PhotoPipeline/1.0 (+contact: ops@glasshouserealty.com)'
DELAY_BETWEEN    = 1.0

EXCLUDED_SOURCES = ('boldtrail-cleveland', 'cleveland-spreadsheet')


# ── UTILITIES ────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def url_hash(url):
    """
    Short stable hash for change detection. Empty input → empty output.
    Returns 12 hex chars (sufficient for our scale).
    """
    if not url:
        return ''
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]


def agent_key(agent):
    """
    Stable unique key for filename derivation.

    Priority:
      1. boldtrailId (the dominant case)
      2. email-{12hex} for agents without a BT id (joint listings)
      3. None when neither is available

    Email is lowercased before hashing for case-insensitivity.
    Whitespace-only values are treated as missing.
    """
    btid = (agent.get('boldtrailId') or '').strip()
    if btid:
        return btid
    email = (agent.get('email') or '').strip().lower()
    if email:
        return 'email-' + hashlib.sha256(email.encode('utf-8')).hexdigest()[:12]
    return None


def photo_file_path(key):
    return os.path.join(PHOTO_DIR, f'{key}.jpg')


def photo_public_url(key):
    return f'{PHOTO_PUBLIC_BASE}/{key}.jpg'


def is_self_hosted(url):
    if not url:
        return False
    return url.startswith(PHOTO_PUBLIC_BASE)


# ── STATE ────────────────────────────────────────────────────────────────────
def load_state():
    """
    Load pipeline state. Returns {'runCounter': int, 'agentRetries': dict}.
    Missing/corrupt file → defaults (fail open).
    """
    if not os.path.exists(STATE_FILE):
        return {'runCounter': 0, 'agentRetries': {}}
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if 'runCounter' not in data:
            data['runCounter'] = 0
        if 'agentRetries' not in data:
            data['agentRetries'] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {'runCounter': 0, 'agentRetries': {}}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, sort_keys=True)


def in_cooldown(state, key, run_id):
    """
    Every-other-sync retry pattern: after a failure, skip the next sync,
    retry on the one after, etc. (alternating).

    diff = run_id - lastAttemptRun. Odd diff → cooldown.

    Defensive: returns False if no retry record exists or run_id is None.
    """
    if run_id is None:
        return False
    rec = state.get('agentRetries', {}).get(key)
    if not rec:
        return False
    last_run = rec.get('lastAttemptRun')
    if last_run is None:
        return False
    diff = run_id - last_run
    if diff <= 0:
        return False
    return (diff % 2) == 1


def record_attempt(state, key, run_id, success, reason=''):
    """Record an acquisition attempt in pipeline state."""
    retries = state.setdefault('agentRetries', {})
    if success:
        retries.pop(key, None)
    else:
        prev = retries.get(key, {})
        retries[key] = {
            'lastAttemptRun':    run_id,
            'lastAttemptAt':     now_iso(),
            'lastFailedReason':  reason,
            'failureCount':      prev.get('failureCount', 0) + 1,
        }


# ── ELIGIBILITY ──────────────────────────────────────────────────────────────
def needs_acquisition(agent, key):
    """
    Decide whether this agent needs photo work this sync.

    Returns (yes, reason):
      ineligible reasons: hidden, soft_deleted, cleveland_excluded, no_key
      acquire reasons:    no_self_hosted_photo, boldtrail_changed,
                          upgrade_to_boldtrail
      skip reason:        fresh
    """
    if agent.get('hidden'):
        return False, 'hidden'
    if agent.get('softDeletedAt'):
        return False, 'soft_deleted'
    if agent.get('source', '') in EXCLUDED_SOURCES:
        return False, 'cleveland_excluded'
    if not key:
        return False, 'no_key'

    photo = agent.get('photo', '') or ''
    photo_source = agent.get('photoSource', '') or ''
    photo_hash = agent.get('photoSourceHash', '') or ''

    bt_added = bool(agent.get('boldtrailAvatarAdded'))
    bt_url = agent.get('boldtrailPhoto', '') or ''

    # Photo missing OR pointing at external URL OR file is gone from disk
    if not photo or not is_self_hosted(photo):
        return True, 'no_self_hosted_photo'
    if not os.path.exists(photo_file_path(key)):
        return True, 'no_self_hosted_photo'

    # BoldTrail has a photo — check if we need to re-acquire
    if bt_added and bt_url:
        if photo_source != 'boldtrail':
            return True, 'upgrade_to_boldtrail'
        if photo_hash != url_hash(bt_url):
            return True, 'boldtrail_changed'

    return False, 'fresh'


# ── LOFTY PAGE PARSER ────────────────────────────────────────────────────────
AGENT_CARD_OPEN = re.compile(r'<div\s+class="agent-card"', re.IGNORECASE)
IMG_TAG_SRC_FIRST = re.compile(
    r'<img\b[^>]*?\bsrc="(https://[^"]+)"[^>]*?\balt="([^"]*)"',
    re.IGNORECASE | re.DOTALL,
)
IMG_TAG_ALT_FIRST = re.compile(
    r'<img\b[^>]*?\balt="([^"]*)"[^>]*?\bsrc="(https://[^"]+)"',
    re.IGNORECASE | re.DOTALL,
)


def extract_lofty_photo_url(html, expected_name):
    """
    Parse Lofty agent page HTML for the headshot URL.

    Returns (url, reason). reason is None on success, otherwise one of:
      empty_html
      no_agent_card_div       (custom page like Scottie's — rejected)
      no_img_in_agent_card
      alt_name_mismatch:...
      non_lofty_cdn:...
    """
    if not html:
        return None, 'empty_html'

    m = AGENT_CARD_OPEN.search(html)
    if not m:
        return None, 'no_agent_card_div'

    card_html = html[m.start():]

    img_match = IMG_TAG_SRC_FIRST.search(card_html)
    if img_match:
        src, alt = img_match.group(1), img_match.group(2)
    else:
        img_match = IMG_TAG_ALT_FIRST.search(card_html)
        if img_match:
            alt, src = img_match.group(1), img_match.group(2)
        else:
            return None, 'no_img_in_agent_card'

    alt = html_module.unescape(alt or '').strip()

    if not any(host in src for host in LOFTY_CDN_HOSTS):
        return None, f'non_lofty_cdn:{src[:60]}'

    # Name validation: exact match OR shared name token (handles nicknames)
    expected_lower = (expected_name or '').lower().strip()
    alt_lower = alt.lower()
    if alt_lower and alt_lower != expected_lower:
        expected_tokens = set(expected_lower.split())
        alt_tokens = set(alt_lower.split())
        if not (expected_tokens & alt_tokens):
            return None, f'alt_name_mismatch:expected={expected_name!r},got={alt!r}'

    return src, None


# ── HTTP ─────────────────────────────────────────────────────────────────────
def download_bytes(url):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r:
        return r.read()


def fetch_html(url):
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent':      USER_AGENT,
            'Accept':          'text/html,application/xhtml+xml',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT) as resp:
            raw = resp.read()
            try:
                return raw.decode('utf-8')
            except UnicodeDecodeError:
                return raw.decode('latin-1')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


# ── IMAGE OPTIMIZE ───────────────────────────────────────────────────────────
def optimize_image(raw_bytes):
    """Optimize raw image bytes to a web-friendly JPEG."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)

    if img.mode != 'RGB':
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            mask = img.split()[-1] if img.mode in ('RGBA', 'LA') else None
            bg.paste(img, mask=mask)
            img = bg
        else:
            img = img.convert('RGB')

    w, h = img.size
    if max(w, h) > MAX_DIMENSION:
        if w > h:
            img = img.resize((MAX_DIMENSION, int(h * MAX_DIMENSION / w)), Image.LANCZOS)
        else:
            img = img.resize((int(w * MAX_DIMENSION / h), MAX_DIMENSION), Image.LANCZOS)

    for quality in (85, 80, 75, 70, 65, 60, 55, 50):
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=quality, optimize=True, progressive=True)
        if buf.tell() / 1024 <= TARGET_KB or quality == 50:
            return buf.getvalue()

    return buf.getvalue()


# ── SOURCE A: BOLDTRAIL ──────────────────────────────────────────────────────
def try_source_a(agent):
    """
    Attempt acquisition from BoldTrail S3.

    Returns dict: {success: bool, reason: str, sourceUrl?: str, bytes?: bytes}

    Failure reasons:
      avatar_added_false, no_bt_url, invalid_bt_url,
      download_failed:<exc>, response_too_small, optimize_failed:<exc>
    """
    bt_added = bool(agent.get('boldtrailAvatarAdded'))
    bt_url = (agent.get('boldtrailPhoto') or '').strip()

    if not bt_added:
        return {'success': False, 'reason': 'avatar_added_false'}
    if not bt_url:
        return {'success': False, 'reason': 'no_bt_url'}
    if not bt_url.startswith('http'):
        return {'success': False, 'reason': 'invalid_bt_url'}

    try:
        raw = download_bytes(bt_url)
    except Exception as e:
        return {'success': False,
                'reason': f'download_failed:{type(e).__name__}',
                'sourceUrl': bt_url}

    if not raw or len(raw) < 100:
        return {'success': False, 'reason': 'response_too_small', 'sourceUrl': bt_url}

    try:
        optimized = optimize_image(raw)
    except Exception as e:
        return {'success': False,
                'reason': f'optimize_failed:{type(e).__name__}',
                'sourceUrl': bt_url}

    return {'success': True, 'reason': '', 'sourceUrl': bt_url, 'bytes': optimized}


# ── SOURCE B: LOFTY ──────────────────────────────────────────────────────────
def try_source_b(agent):
    """
    Attempt acquisition by scraping Lofty agent profile page.

    Failure reasons:
      no_profile_url, page_fetch_failed, parse:<reason>,
      photo_download_failed:<exc>, response_too_small, optimize_failed:<exc>
    """
    profile_url = (agent.get('profileUrl') or '').strip()
    if not profile_url:
        return {'success': False, 'reason': 'no_profile_url'}

    time.sleep(DELAY_BETWEEN)
    html = fetch_html(profile_url)
    if html is None:
        return {'success': False, 'reason': 'page_fetch_failed',
                'sourceUrl': profile_url}

    scraped_url, parse_reason = extract_lofty_photo_url(html, agent.get('name', ''))
    if not scraped_url:
        return {'success': False, 'reason': f'parse:{parse_reason}',
                'sourceUrl': profile_url}

    try:
        raw = download_bytes(scraped_url)
    except Exception as e:
        return {'success': False,
                'reason': f'photo_download_failed:{type(e).__name__}',
                'sourceUrl': scraped_url}

    if not raw or len(raw) < 100:
        return {'success': False, 'reason': 'response_too_small',
                'sourceUrl': scraped_url}

    try:
        optimized = optimize_image(raw)
    except Exception as e:
        return {'success': False,
                'reason': f'optimize_failed:{type(e).__name__}',
                'sourceUrl': scraped_url}

    return {'success': True, 'reason': '', 'sourceUrl': scraped_url, 'bytes': optimized}


# ── ORPHAN CLEANUP ───────────────────────────────────────────────────────────
def should_run_orphan_cleanup():
    """Returns (should_run, today_date_str)."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if not os.path.exists(ORPHAN_MARKER):
        return True, today
    try:
        with open(ORPHAN_MARKER) as f:
            last = f.read().strip()
        return (last != today), today
    except OSError:
        return True, today


def mark_orphan_cleanup_done(today):
    with open(ORPHAN_MARKER, 'w') as f:
        f.write(today)


def run_orphan_cleanup(agents, dry_run=False):
    """Delete .jpg files in PHOTO_DIR for which no agent_key matches. Returns int."""
    if not os.path.isdir(PHOTO_DIR):
        return 0

    valid_keys = set()
    for a in agents:
        k = agent_key(a)
        if k:
            valid_keys.add(k)

    removed = 0
    for fname in os.listdir(PHOTO_DIR):
        if not fname.endswith('.jpg'):
            continue
        key_from_file = fname[:-4]
        if key_from_file not in valid_keys:
            if not dry_run:
                try:
                    os.remove(os.path.join(PHOTO_DIR, fname))
                except OSError:
                    continue
            removed += 1
    return removed


# ── REPORT ───────────────────────────────────────────────────────────────────
def write_report(events, dry_run):
    os.makedirs(REPORT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
    filename = f'{REPORT_DIR}/photo-pipeline-{timestamp}.json'
    payload = {
        'generatedAt':    now_iso(),
        'mode':           'dry-run' if dry_run else 'LIVE',
        'totalEvents':    len(events),
        'acquired_a':     sum(1 for e in events if e.get('source') == 'boldtrail'),
        'acquired_b':     sum(1 for e in events if e.get('source') == 'lofty'),
        'kept_existing':  sum(1 for e in events if e.get('status') in ('kept_existing', 'kept_existing_boldtrail')),
        'failed':         sum(1 for e in events if e.get('status') == 'failed'),
        'cooldown':       sum(1 for e in events if e.get('status') == 'lofty_cooldown'),
        'events':         events,
    }
    with open(filename, 'w') as f:
        json.dump(payload, f, indent=2)
    return filename


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Glasshouse photo acquisition pipeline')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int, default=0,
                        help='Cap agents processed (0=no limit)')
    parser.add_argument('--force', metavar='NAME',
                        help='Force-acquire for this name even if fresh')
    parser.add_argument('--agents-file', default=AGENTS_FILE)
    parser.add_argument('--skip-cleanup', action='store_true')
    args = parser.parse_args()

    agents_file = args.agents_file

    print('=' * 60)
    print('Glasshouse Photo Acquisition Pipeline')
    print(f'Timestamp:   {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}')
    print(f'Mode:        {"DRY-RUN" if args.dry_run else "LIVE"}')
    print(f'Agents file: {agents_file}')
    print('=' * 60)
    print('NOTE: BoldTrail/Lofty accessed READ-ONLY. No writes upstream.')
    print('=' * 60)

    if not os.path.exists(agents_file):
        print(f'ERROR: {agents_file} not found')
        sys.exit(0)
    with open(agents_file) as f:
        agents = json.load(f)
    print(f'  Loaded {len(agents)} agents from {agents_file}')

    state = load_state()
    state['runCounter'] = state.get('runCounter', 0) + 1
    run_id = state['runCounter']
    print(f'  Pipeline state: run #{run_id}, {len(state["agentRetries"])} tracked retries')

    os.makedirs(PHOTO_DIR, exist_ok=True)

    # ── Phase 1: identify work ─────────────────────────────────────────────
    print(f'\n── Identifying Work ───────────────────────────────────')
    work_queue = []
    skip_counts = {}

    for a in agents:
        key = agent_key(a)
        needs, reason = needs_acquisition(a, key)
        is_forced = args.force and a.get('name', '').lower() == args.force.lower()

        if not needs and not is_forced:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1
            continue

        if not key:
            skip_counts['no_key'] = skip_counts.get('no_key', 0) + 1
            continue

        work_queue.append((a, key, 'forced' if is_forced else reason))

    print(f'  Work queue:           {len(work_queue)} agents')
    if skip_counts:
        for reason, count in sorted(skip_counts.items()):
            print(f'    {reason:30s} {count}')

    if args.limit > 0 and len(work_queue) > args.limit:
        print(f'\n  --limit {args.limit} applied: processing {args.limit}')
        work_queue = work_queue[:args.limit]

    # ── Phase 2: acquire ───────────────────────────────────────────────────
    print(f'\n── Acquiring ──────────────────────────────────────────')
    events = []
    photos_written = 0
    agents_updated = 0

    for i, (agent, key, why) in enumerate(work_queue, 1):
        name = agent.get('name', '?')
        event = {
            'name':         name,
            'key':          key,
            'reason':       why,
            'attemptedAt':  now_iso(),
        }

        result_a = try_source_a(agent)
        if result_a['success']:
            event['status']    = 'success'
            event['source']    = 'boldtrail'
            event['sourceUrl'] = result_a['sourceUrl']
            if not args.dry_run:
                with open(photo_file_path(key), 'wb') as f:
                    f.write(result_a['bytes'])
                agent['photo']           = photo_public_url(key)
                agent['photoSource']     = 'boldtrail'
                agent['photoSourceHash'] = url_hash(result_a['sourceUrl'])
                record_attempt(state, key, run_id, success=True)
                photos_written += 1
                agents_updated += 1
            print(f'  [{i}/{len(work_queue)}] {name}: ✓ BoldTrail '
                  f'({len(result_a["bytes"])} bytes)')
            events.append(event)
            continue

        event['boldtrailFailure'] = result_a['reason']

        # Don't downgrade if Source A failed AND existing is BoldTrail
        if agent.get('photoSource') == 'boldtrail':
            event['status'] = 'kept_existing_boldtrail'
            print(f'  [{i}/{len(work_queue)}] {name}: − BoldTrail '
                  f'failed ({result_a["reason"]}), keeping existing')
            events.append(event)
            continue

        # Cooldown gate before Source B
        if in_cooldown(state, key, run_id):
            event['status'] = 'lofty_cooldown'
            print(f'  [{i}/{len(work_queue)}] {name}: ⏸ Lofty cooldown')
            events.append(event)
            continue

        result_b = try_source_b(agent)
        if result_b['success']:
            event['status']    = 'success'
            event['source']    = 'lofty'
            event['sourceUrl'] = result_b['sourceUrl']
            if not args.dry_run:
                with open(photo_file_path(key), 'wb') as f:
                    f.write(result_b['bytes'])
                agent['photo']           = photo_public_url(key)
                agent['photoSource']     = 'lofty'
                agent['photoSourceHash'] = url_hash(result_b['sourceUrl'])
                record_attempt(state, key, run_id, success=True)
                photos_written += 1
                agents_updated += 1
            print(f'  [{i}/{len(work_queue)}] {name}: ✓ Lofty '
                  f'({len(result_b["bytes"])} bytes)')
            events.append(event)
            continue

        event['loftyFailure'] = result_b['reason']

        # Source C: keep existing self-hosted
        existing = agent.get('photo', '')
        if is_self_hosted(existing) and os.path.exists(photo_file_path(key)):
            event['status'] = 'kept_existing'
            print(f'  [{i}/{len(work_queue)}] {name}: − Both failed, keeping existing')
            if not args.dry_run:
                record_attempt(state, key, run_id, success=False,
                               reason=f'A:{result_a["reason"]},B:{result_b["reason"]}')
            events.append(event)
            continue

        # Source D: nothing worked
        event['status'] = 'failed'
        if not args.dry_run:
            if agent.get('photo') and not is_self_hosted(agent['photo']):
                agent['photo']           = ''
                agent['photoSource']     = ''
                agent['photoSourceHash'] = ''
                agents_updated += 1
            record_attempt(state, key, run_id, success=False,
                           reason=f'A:{result_a["reason"]},B:{result_b["reason"]}')
        print(f'  [{i}/{len(work_queue)}] {name}: ✗ both sources failed')
        events.append(event)

    # ── Phase 3: strip temp fields, write outputs ──────────────────────────
    print(f'\n── Writing Outputs ────────────────────────────────────')
    for a in agents:
        a.pop('boldtrailPhoto', None)
        a.pop('boldtrailAvatarAdded', None)

    if not args.dry_run:
        with open(agents_file, 'w') as f:
            json.dump(agents, f, indent=2)
        print(f'  ✓ Updated {agents_file} '
              f'(temp fields stripped, {agents_updated} records changed)')
        save_state(state)
        print(f'  ✓ State saved (run #{run_id}, '
              f'{len(state["agentRetries"])} tracked)')
        if photos_written:
            print(f'  ✓ Wrote {photos_written} optimized photos to {PHOTO_DIR}/')
    else:
        print(f'  No file writes (dry-run)')

    # ── Phase 4: orphan cleanup (once per day) ─────────────────────────────
    if not args.dry_run and not args.skip_cleanup:
        should_clean, today = should_run_orphan_cleanup()
        if should_clean:
            print(f'\n── Orphan Cleanup ─────────────────────────────────────')
            deleted = run_orphan_cleanup(agents, dry_run=False)
            mark_orphan_cleanup_done(today)
            print(f'  ✓ Removed {deleted} orphaned photo files')

    # ── Report ─────────────────────────────────────────────────────────────
    report_path = write_report(events, args.dry_run)
    print(f'\n  ✓ Report: {report_path}')

    # ── Summary ────────────────────────────────────────────────────────────
    a_count = sum(1 for e in events if e.get('source') == 'boldtrail')
    b_count = sum(1 for e in events if e.get('source') == 'lofty')
    kept    = sum(1 for e in events if e.get('status') in ('kept_existing', 'kept_existing_boldtrail'))
    cool    = sum(1 for e in events if e.get('status') == 'lofty_cooldown')
    failed  = sum(1 for e in events if e.get('status') == 'failed')

    print(f'\n── Summary ────────────────────────────────────────────')
    print(f'  Queued:             {len(work_queue)}')
    print(f'  Acquired BoldTrail: {a_count}')
    print(f'  Acquired Lofty:     {b_count}')
    print(f'  Kept existing:      {kept}')
    print(f'  Lofty cooldown:     {cool}')
    print(f'  Failed:             {failed}')

    print('=' * 60)
    print('PIPELINE COMPLETE')
    print('=' * 60)


if __name__ == '__main__':
    main()
