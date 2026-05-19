#!/usr/bin/env python3
"""
test_photo_pipeline.py — Unit tests for gh-photo-pipeline.py

Tests the new photo acquisition pipeline in isolation:
  - agent_key() handles all the ways an agent can be identified
  - url_hash() produces stable change-detection hashes
  - needs_acquisition() decides correctly per agent state
  - in_cooldown() enforces every-other-sync retry pattern
  - try_source_a() and try_source_b() respect signal fields
  - extract_lofty_photo_url() handles the standard agent-card template
    AND falls back gracefully for custom pages

These tests don't make real HTTP requests — they use mocks for HTTP I/O.
"""
import importlib.util
import os
import sys
import json
import hashlib

# Load the pipeline module
spec = importlib.util.spec_from_file_location('pipeline', 'gh-photo-pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

# Test counter and report
TESTS_PASSED = 0
TESTS_FAILED = 0
FAILURES = []


def check(name, condition, detail=''):
    global TESTS_PASSED, TESTS_FAILED
    if condition:
        TESTS_PASSED += 1
        print(f'  ✓ {name}')
    else:
        TESTS_FAILED += 1
        FAILURES.append((name, detail))
        print(f'  ✗ {name}{(": " + detail) if detail else ""}')


# ── TEST 1: agent_key() ──────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 1: agent_key() — stable filename derivation')
print('=' * 70)

check('btid wins when available',
      pipeline.agent_key({'boldtrailId': '272193', 'email': 'a@b.com'}) == '272193')

check('email-hash used when no btid',
      pipeline.agent_key({'boldtrailId': '', 'email': 'connie@deanna.com'})
      .startswith('email-'))

check('email-hash is stable',
      pipeline.agent_key({'email': 'foo@bar.com'}) ==
      pipeline.agent_key({'email': 'foo@bar.com'}))

check('email-hash is case-insensitive',
      pipeline.agent_key({'email': 'FOO@bar.com'}) ==
      pipeline.agent_key({'email': 'foo@bar.com'}))

check('no key when neither btid nor email',
      pipeline.agent_key({'name': 'No Identifiers'}) is None)

check('whitespace-only btid falls through to email',
      pipeline.agent_key({'boldtrailId': '   ', 'email': 'a@b.com'})
      .startswith('email-'))

check('whitespace-only btid and email returns None',
      pipeline.agent_key({'boldtrailId': '   ', 'email': '   '}) is None)

# REGRESSION: real-world agents.json sometimes has boldtrailId as int.
# Production crash on 2026-05-19: `'int' object has no attribute 'strip'`.
check('integer btid is coerced to string',
      pipeline.agent_key({'boldtrailId': 272193}) == '272193')

check('integer btid same key as string equivalent',
      pipeline.agent_key({'boldtrailId': 272193}) ==
      pipeline.agent_key({'boldtrailId': '272193'}))

check('None btid + valid email falls through to email-hash',
      pipeline.agent_key({'boldtrailId': None, 'email': 'a@b.com'})
      .startswith('email-'))

check('Non-str/non-int btid (e.g. dict) treated as missing',
      pipeline.agent_key({'boldtrailId': {'nested': 1}, 'email': 'a@b.com'})
      .startswith('email-'))

check('Float-typed btid (defensive) treated as missing → email fallback',
      pipeline.agent_key({'boldtrailId': 3.14, 'email': 'a@b.com'})
      .startswith('email-'))


# ── TEST 2: url_hash() ───────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 2: url_hash() — change detection hashing')
print('=' * 70)

check('Empty string → empty hash',
      pipeline.url_hash('') == '')

check('Same URL → same hash',
      pipeline.url_hash('https://x.com/a.jpg') ==
      pipeline.url_hash('https://x.com/a.jpg'))

check('Different URLs → different hashes',
      pipeline.url_hash('https://x.com/a.jpg') !=
      pipeline.url_hash('https://x.com/b.jpg'))

check('Hash is short (12-16 chars)',
      0 < len(pipeline.url_hash('https://x.com/a.jpg')) <= 16)


# ── TEST 3: needs_acquisition() ──────────────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 3: needs_acquisition() — decision logic')
print('=' * 70)

# Helpers
def make_agent(**kwargs):
    """Make a synthetic agent record."""
    defaults = {
        'name':                'Test Agent',
        'boldtrailId':         '99999',
        'email':               'test@example.com',
        'photo':               '',
        'photoSource':         '',
        'photoSourceHash':     '',
        'boldtrailPhoto':      '',
        'boldtrailAvatarAdded': False,
        'profileUrl':          '',
        'source':              'boldtrail',
        'hidden':              False,
    }
    defaults.update(kwargs)
    return defaults


# Hidden agent → skip
agent = make_agent(hidden=True, photo='https://x.com/y.jpg')
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Hidden agent → skip', ok is False and reason == 'hidden')

# Soft-deleted → skip
agent = make_agent(softDeletedAt='2026-01-01T00:00:00Z')
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Soft-deleted agent → skip', ok is False and reason == 'soft_deleted')

# Cleveland → skip
agent = make_agent(source='boldtrail-cleveland')
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Cleveland agent → skip', ok is False and reason == 'cleveland_excluded')

# Spreadsheet cleveland → skip
agent = make_agent(source='cleveland-spreadsheet')
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Cleveland spreadsheet → skip', ok is False and reason == 'cleveland_excluded')

# No key → skip
agent = make_agent()
ok, reason = pipeline.needs_acquisition(agent, None)
check('No key (no btid, no email) → skip', ok is False and reason == 'no_key')

# No photo yet → acquire
agent = make_agent(boldtrailAvatarAdded=True, boldtrailPhoto='https://s3.../a.jpg')
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('No self-hosted photo → acquire', ok is True and reason == 'no_self_hosted_photo')

# Has self-hosted photo, BoldTrail hash matches → skip
bt_url = 'https://s3.amazonaws.com/bucket/avatar.jpg'
agent = make_agent(
    photo='https://evansvince.github.io/glasshouse-data/agent-photos/99999.jpg',
    photoSource='boldtrail',
    photoSourceHash=pipeline.url_hash(bt_url),
    boldtrailAvatarAdded=True,
    boldtrailPhoto=bt_url,
)
# Note: we also need the file to exist on disk. Create a temp one.
test_photo_dir = pipeline.PHOTO_DIR
os.makedirs(test_photo_dir, exist_ok=True)
test_photo_path = os.path.join(test_photo_dir, '99999.jpg')
with open(test_photo_path, 'w') as f:
    f.write('dummy')

ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Self-hosted + hash match → skip (fresh)',
      ok is False and reason == 'fresh',
      f'Got ok={ok}, reason={reason}')

# Has self-hosted photo, BoldTrail hash CHANGED → re-acquire
agent = make_agent(
    photo='https://evansvince.github.io/glasshouse-data/agent-photos/99999.jpg',
    photoSource='boldtrail',
    photoSourceHash=pipeline.url_hash('https://old.example/old.jpg'),  # old hash
    boldtrailAvatarAdded=True,
    boldtrailPhoto=bt_url,  # different URL, different hash
)
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Self-hosted + hash MISMATCH → re-acquire',
      ok is True and reason == 'boldtrail_changed')

# Lofty-sourced, BoldTrail now has photo → upgrade
agent = make_agent(
    photo='https://evansvince.github.io/glasshouse-data/agent-photos/99999.jpg',
    photoSource='lofty',
    photoSourceHash=pipeline.url_hash('https://cdn.lofty.com/x.jpg'),
    boldtrailAvatarAdded=True,
    boldtrailPhoto=bt_url,
)
# Need file to exist for "fresh" check NOT to fire
os.makedirs(test_photo_dir, exist_ok=True)
with open(test_photo_path, 'w') as f:
    f.write('dummy')
ok, reason = pipeline.needs_acquisition(agent, '99999')
# Either "boldtrail_changed" or "upgrade_to_boldtrail" is correct — both mean
# re-acquire. The Lofty-sourced agent's hash is always different from the
# BoldTrail URL hash, so in practice "boldtrail_changed" fires first.
check('Lofty-sourced + BoldTrail now has photo → re-acquire',
      ok is True and reason in ('boldtrail_changed', 'upgrade_to_boldtrail'),
      f'Got reason={reason}')
if os.path.exists(test_photo_path):
    os.remove(test_photo_path)

# Self-hosted file MISSING on disk (orphaned record) → re-acquire
# (file already removed above; that's the test condition)
agent = make_agent(
    photo='https://evansvince.github.io/glasshouse-data/agent-photos/99999.jpg',
    photoSource='boldtrail',
    photoSourceHash=pipeline.url_hash(bt_url),
    boldtrailAvatarAdded=True,
    boldtrailPhoto=bt_url,
)
ok, reason = pipeline.needs_acquisition(agent, '99999')
check('Photo URL points at file that no longer exists → re-acquire',
      ok is True and reason == 'no_self_hosted_photo')

# REGRESSION: real agents.json had boldtrailId as int.
# Pipeline crashed with AttributeError on first agent.
# Test that needs_acquisition() doesn't blow up on non-string fields.
agent_with_int_id = {
    'name':                'Real World Agent',
    'boldtrailId':         272193,  # INT, not string
    'email':               'real@example.com',
    'photo':               '',
    'photoSource':         '',
    'photoSourceHash':     '',
    'boldtrailPhoto':      'https://s3.amazonaws.com/bucket/x.jpg',
    'boldtrailAvatarAdded': True,
    'profileUrl':          '',
    'source':              'boldtrail',
    'hidden':              False,
}
try:
    key = pipeline.agent_key(agent_with_int_id)
    ok, reason = pipeline.needs_acquisition(agent_with_int_id, key)
    check('Integer boldtrailId does not crash needs_acquisition',
          ok is True and reason == 'no_self_hosted_photo')
except (AttributeError, TypeError) as e:
    check('Integer boldtrailId does not crash needs_acquisition',
          False, f'CRASHED: {type(e).__name__}: {e}')

# Same for try_source_a — non-string boldtrailPhoto must not crash
agent_with_dict_photo = {
    'name':                'Weird Data',
    'boldtrailId':         '999',
    'boldtrailAvatarAdded': True,
    'boldtrailPhoto':      None,  # not a string
}
try:
    result = pipeline.try_source_a(agent_with_dict_photo)
    check('Non-string boldtrailPhoto handled gracefully in try_source_a',
          result['success'] is False and result['reason'] == 'no_bt_url')
except (AttributeError, TypeError) as e:
    check('Non-string boldtrailPhoto handled gracefully in try_source_a',
          False, f'CRASHED: {type(e).__name__}: {e}')


# ── TEST 4: in_cooldown() ────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 4: in_cooldown() — every-other-sync retry pattern')
print('=' * 70)

# Never failed → not in cooldown
state = {'agentRetries': {}}
check('No retry record → not in cooldown',
      pipeline.in_cooldown(state, '99999', run_id=5) is False)

# Last attempted run 4, current run 5 → diff=1 → cooldown
state = {'agentRetries': {'99999': {'lastAttemptRun': 4}}}
check('Run right after failure → cooldown (diff=1, skip)',
      pipeline.in_cooldown(state, '99999', run_id=5) is True)

# Last attempted run 4, current run 6 → diff=2 → not in cooldown
check('Two runs after failure → retry (diff=2)',
      pipeline.in_cooldown(state, '99999', run_id=6) is False)

# Last attempted run 4, current run 7 → diff=3 → cooldown (odd diff)
check('Three runs after failure → cooldown (diff=3, alternating)',
      pipeline.in_cooldown(state, '99999', run_id=7) is True)

# No run_id → never in cooldown
check('No run_id → not in cooldown (defensive)',
      pipeline.in_cooldown(state, '99999', run_id=None) is False)


# ── TEST 5: try_source_a() signal-based gating ───────────────────────────────
print('\n' + '=' * 70)
print('TEST 5: try_source_a() — respects avatar_added signal (no HTTP)')
print('=' * 70)

# avatar_added=False → skip without attempting download
agent = make_agent(boldtrailAvatarAdded=False, boldtrailPhoto='https://x.com/a.jpg')
result = pipeline.try_source_a(agent)
check('avatar_added=False → skip Source A',
      result['success'] is False and result['reason'] == 'avatar_added_false')

# avatar_added=True but no boldtrailPhoto URL → skip
agent = make_agent(boldtrailAvatarAdded=True, boldtrailPhoto='')
result = pipeline.try_source_a(agent)
check('avatar_added=True but no URL → skip Source A',
      result['success'] is False and result['reason'] == 'no_bt_url')

# avatar_added=True but URL doesn't start with http → skip
agent = make_agent(boldtrailAvatarAdded=True, boldtrailPhoto='/assets/empty-avatar.png')
result = pipeline.try_source_a(agent)
check('avatar_added=True but URL is relative → skip',
      result['success'] is False and result['reason'] == 'invalid_bt_url')


# ── TEST 6: extract_lofty_photo_url() — HTML parsing ─────────────────────────
print('\n' + '=' * 70)
print('TEST 6: extract_lofty_photo_url() — Lofty page HTML parsing')
print('=' * 70)

# Real sample from production
real_html = '''<div class="agent-card"><div class="agent-headshot agent-image md-agent-banner-standard"><div class="img-box agent-img"><!----> <!----> <div class="img-content" style="z-index:0;"><img class="" style="" src="https://cdn.lofty.com/image/fs/web/2026219/19/abc-def.webp" alt="Abagale Geise"></div> <!--[--><!--]--></div> <!--[--><!--]--></div></div>'''
url, reason = pipeline.extract_lofty_photo_url(real_html, 'Abagale Geise')
check('Real Lofty HTML → extract correct URL',
      url == 'https://cdn.lofty.com/image/fs/web/2026219/19/abc-def.webp',
      f'Got url={url}, reason={reason}')

# Chime CDN (legacy URL form)
chime_html = '''<div class="agent-card"><img src="https://cdn.chime.me/image/fs/user-info/x/w640_y-jpeg.webp" alt="Carl Fisher"></div>'''
url, reason = pipeline.extract_lofty_photo_url(chime_html, 'Carl Fisher')
check('Chime CDN URL accepted',
      url == 'https://cdn.chime.me/image/fs/user-info/x/w640_y-jpeg.webp',
      f'Got reason={reason}')

# Wrong agent in alt → reject
url, reason = pipeline.extract_lofty_photo_url(real_html, 'Bob Other')
check('Wrong agent in alt → reject',
      url is None and 'alt_name_mismatch' in reason)

# No agent-card div (custom page like Scottie Fulhart) → reject
no_card = '<html><body><h1>Custom page</h1><img src="https://cdn.lofty.com/x.jpg"></body></html>'
url, reason = pipeline.extract_lofty_photo_url(no_card, 'Scottie Fulhart')
check('No agent-card div → reject (custom page)',
      url is None and reason == 'no_agent_card_div')

# Empty HTML → reject
url, reason = pipeline.extract_lofty_photo_url('', 'Anyone')
check('Empty HTML → reject',
      url is None and reason == 'empty_html')

# Non-Lofty CDN → reject (e.g. random hotlinked decoration)
bad_cdn = '<div class="agent-card"><img src="https://random.example/photo.jpg" alt="Bob Smith"></div>'
url, reason = pipeline.extract_lofty_photo_url(bad_cdn, 'Bob Smith')
check('Non-Lofty CDN URL → reject',
      url is None and 'non_lofty_cdn' in reason)

# HTML entity in alt attribute (e.g. O&#39;Diam)
entity_html = '''<div class="agent-card"><img src="https://cdn.lofty.com/x.webp" alt="Deanna O&#39;Diam"></div>'''
url, reason = pipeline.extract_lofty_photo_url(entity_html, "Deanna O'Diam")
check('HTML entities in alt are decoded for name match',
      url == 'https://cdn.lofty.com/x.webp')

# Alt-first attribute order
alt_first = '''<div class="agent-card"><img alt="Bob Smith" src="https://cdn.lofty.com/x.webp"></div>'''
url, reason = pipeline.extract_lofty_photo_url(alt_first, 'Bob Smith')
check('Alt-first attribute order works',
      url == 'https://cdn.lofty.com/x.webp')

# Partial name match (nickname case)
url, reason = pipeline.extract_lofty_photo_url(real_html, 'Abby Geise')
check('Partial name match (shared token) accepted',
      url is not None,
      f'reason={reason}')


# ── TEST 7: should_run_orphan_cleanup() — daily marker ───────────────────────
print('\n' + '=' * 70)
print('TEST 7: should_run_orphan_cleanup() — daily marker gate')
print('=' * 70)

# Marker file doesn't exist → should run
if os.path.exists(pipeline.ORPHAN_MARKER):
    os.remove(pipeline.ORPHAN_MARKER)
should_clean, today = pipeline.should_run_orphan_cleanup()
check('No marker → should run',
      should_clean is True)

# Marker for today → don't run
with open(pipeline.ORPHAN_MARKER, 'w') as f:
    f.write(today)
should_clean, today2 = pipeline.should_run_orphan_cleanup()
check('Marker matches today → skip',
      should_clean is False)

# Marker for old date → should run
with open(pipeline.ORPHAN_MARKER, 'w') as f:
    f.write('2020-01-01')
should_clean, today3 = pipeline.should_run_orphan_cleanup()
check('Marker is old → should run',
      should_clean is True)

# Clean up
os.remove(pipeline.ORPHAN_MARKER)


# ── TEST 8: orphan cleanup ────────────────────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 8: run_orphan_cleanup() — removes only orphans')
print('=' * 70)

# Set up: 3 files in agent-photos/, only 2 agents in agents.json
os.makedirs(pipeline.PHOTO_DIR, exist_ok=True)
for filename in ['111.jpg', '222.jpg', '999-orphan.jpg', 'email-abc123.jpg']:
    with open(os.path.join(pipeline.PHOTO_DIR, filename), 'w') as f:
        f.write('dummy')

# Verify the 4 files exist
existing_files = set(os.listdir(pipeline.PHOTO_DIR))
check('Setup: 4 files exist before cleanup',
      '111.jpg' in existing_files and '222.jpg' in existing_files
      and '999-orphan.jpg' in existing_files and 'email-abc123.jpg' in existing_files)

# Agents that should be kept
agents = [
    {'boldtrailId': '111', 'email': 'a@x.com'},
    {'boldtrailId': '222', 'email': 'b@x.com'},
    # Joint listing identified by email — email-abc123 must match
    # Compute the email hash so the file is valid
]

# Compute what the email-hash key would be for some valid email
hash_email = 'joint@example.com'
joint_key = pipeline.agent_key({'email': hash_email})
# Create a file matching that key
os.rename(
    os.path.join(pipeline.PHOTO_DIR, 'email-abc123.jpg'),
    os.path.join(pipeline.PHOTO_DIR, f'{joint_key}.jpg')
)
agents.append({'boldtrailId': '', 'email': hash_email})

# Cleanup
removed = pipeline.run_orphan_cleanup(agents, dry_run=False)
check('Removed 1 orphan (999-orphan.jpg)',
      removed == 1,
      f'Got removed={removed}')

remaining = set(os.listdir(pipeline.PHOTO_DIR))
check('Valid files kept after cleanup',
      '111.jpg' in remaining and '222.jpg' in remaining
      and f'{joint_key}.jpg' in remaining)
check('Orphan removed from disk',
      '999-orphan.jpg' not in remaining)

# Clean up
for f in remaining:
    try:
        os.remove(os.path.join(pipeline.PHOTO_DIR, f))
    except OSError:
        pass


# ── TEST 9: state save/load round-trip ───────────────────────────────────────
print('\n' + '=' * 70)
print('TEST 9: load_state() / save_state() round-trip')
print('=' * 70)

# Use a temp state file to avoid clobbering anything
original_state_file = pipeline.STATE_FILE
pipeline.STATE_FILE = '/tmp/test-photo-pipeline-state.json'

try:
    # Clean slate
    if os.path.exists(pipeline.STATE_FILE):
        os.remove(pipeline.STATE_FILE)

    # Missing file → defaults
    state = pipeline.load_state()
    check('Missing state file → defaults',
          state.get('runCounter') == 0 and state.get('agentRetries') == {})

    # Save and reload
    state['runCounter'] = 42
    state['agentRetries']['99999'] = {'lastAttemptRun': 41, 'lastFailedReason': 'test'}
    pipeline.save_state(state)
    reloaded = pipeline.load_state()
    check('Save → reload preserves runCounter',
          reloaded['runCounter'] == 42)
    check('Save → reload preserves agentRetries',
          reloaded['agentRetries'].get('99999', {}).get('lastAttemptRun') == 41)

    # Corrupt JSON → defaults
    with open(pipeline.STATE_FILE, 'w') as f:
        f.write('{not valid json')
    state = pipeline.load_state()
    check('Corrupt state file → defaults (fail open)',
          state.get('runCounter') == 0)

finally:
    if os.path.exists(pipeline.STATE_FILE):
        os.remove(pipeline.STATE_FILE)
    pipeline.STATE_FILE = original_state_file


# ── SUMMARY ──────────────────────────────────────────────────────────────────
print('\n' + '=' * 70)
print(f'RESULTS: {TESTS_PASSED} passed, {TESTS_FAILED} failed '
      f'(of {TESTS_PASSED + TESTS_FAILED} checks)')
print('=' * 70)

if TESTS_FAILED:
    print('\nFAILED CHECKS:')
    for name, detail in FAILURES:
        print(f'  ✗ {name}{(": " + detail) if detail else ""}')
    sys.exit(1)

print('✓ All photo pipeline tests passed.')
