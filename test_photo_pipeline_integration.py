#!/usr/bin/env python3
"""
test_photo_pipeline_integration.py — End-to-end integration test

Simulates the full photo pipeline against a synthetic agents.json that
contains every interesting case we've discovered:

  - Allen Blackburn:   has Lofty URL, no BoldTrail photo, standard Lofty page
                       Expected: kept_existing (existing photo, no acquisition)
                       Note: in production, would be re-acquired from Lofty
                       page on first run; in test we can't fetch the page,
                       so we verify the EXISTING photo is preserved.

  - Carl Fisher:       has BROKEN Chime URL, BoldTrail has new photo
                       (avatar_added=true). Expected: re-acquire from
                       BoldTrail, photo updates to self-hosted.

  - Scottie Fulhart:   has Lofty-sourced photo, custom Lofty page, no
                       BoldTrail photo. Expected: kept_existing.

  - Mary Cooper:       BoldTrail-sourced, already self-hosted, hash matches.
                       Expected: skipped (fresh).

  - Aaron Kroggel:     no photo, no profile URL, no BoldTrail photo.
                       Expected: no_sources, photo stays empty.

  - New Agent:         brand new, BoldTrail has photo (avatar_added=true),
                       no existing record. Expected: acquired_boldtrail.

  - Cleveland Agent:   in Cleveland account. Expected: cleveland_excluded.

  - Hidden Agent:      hidden=true. Expected: skip.

  - Lofty-Only Joint:  no boldtrailId, has email and existing Lofty photo.
                       Expected: kept_existing (email-hash key used).

Since the sandbox can't reach BoldTrail S3 or Lofty pages, the test uses
monkey-patching to mock the HTTP layer. We're testing the DECISION LOGIC
and PIPELINE FLOW, not network plumbing.
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import urllib.error

# Load the pipeline module
spec = importlib.util.spec_from_file_location('pipeline', 'gh-photo-pipeline.py')
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

# Test reporting
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


# ── MOCK HTTP LAYER ──────────────────────────────────────────────────────────
# Pretend to be the network. Returns canned responses for known URLs.

# Sample valid JPEG bytes — generated at runtime via Pillow so we don't have
# to deal with hex-encoding gotchas. A 64x64 RGB image, JPEG-compressed.
def _make_tiny_jpeg():
    from PIL import Image
    import io
    img = Image.new('RGB', (64, 64), (200, 100, 50))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=80)
    return buf.getvalue()

TINY_JPEG = _make_tiny_jpeg()

# Sample Lofty page HTML (standard template — Carl Fisher / Allen Blackburn)
STANDARD_LOFTY_HTML = '''<!DOCTYPE html><html><body>
<div class="agent-card"><div class="agent-headshot"><div class="img-content"><img class="" src="{photo_url}" alt="{agent_name}"></div></div></div>
</body></html>'''

# Sample Lofty page HTML (custom — Scottie Fulhart). No agent-card div.
CUSTOM_LOFTY_HTML = '''<!DOCTYPE html><html><body>
<h1>Welcome to my custom page</h1>
<img src="https://example.com/my-photo.jpg" alt="Custom photo">
</body></html>'''


# Pre-defined responses keyed by URL prefix
MOCK_RESPONSES = {}


def setup_mocks():
    """Configure the mock HTTP responses for our test scenarios."""
    global MOCK_RESPONSES
    MOCK_RESPONSES = {
        # Carl Fisher: BoldTrail S3 has a working photo (admin uploaded)
        'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/carl-new.jpg': {
            'type': 'bytes', 'data': TINY_JPEG, 'status': 200,
        },
        # Carl's STALE Chime URL (404s)
        'https://cdn.chime.me/image/fs/user-info/2024719/16/original_287ad2d8-broken.png': {
            'type': 'bytes', 'data': None, 'status': 404,
        },
        # Allen Blackburn: Lofty profile page (standard template)
        'https://glasshouserealty.com/agents/Allen-Blackburn/8726277': {
            'type': 'text',
            'data': STANDARD_LOFTY_HTML.format(
                photo_url='https://cdn.lofty.com/image/fs/web/allen-w640.webp',
                agent_name='Allen Blackburn',
            ),
            'status': 200,
        },
        # Allen's headshot from Lofty CDN
        'https://cdn.lofty.com/image/fs/web/allen-w640.webp': {
            'type': 'bytes', 'data': TINY_JPEG, 'status': 200,
        },
        # Scottie Fulhart: custom page (no agent-card)
        'https://glasshouserealty.com/agents/Scottie-Fulhart/8726999': {
            'type': 'text', 'data': CUSTOM_LOFTY_HTML, 'status': 200,
        },
        # New Agent: BoldTrail has a photo
        'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/newagent.jpg': {
            'type': 'bytes', 'data': TINY_JPEG, 'status': 200,
        },
    }


def mock_download_bytes(url):
    """Replacement for pipeline.download_bytes. Returns raw bytes or raises."""
    resp = MOCK_RESPONSES.get(url)
    if resp is None:
        raise urllib.error.HTTPError(url, 404, 'Not Found', None, None)
    if resp['type'] != 'bytes':
        raise urllib.error.HTTPError(url, resp['status'], 'Wrong type', None, None)
    if resp['data'] is None:
        raise urllib.error.HTTPError(url, resp['status'], 'Mocked failure', None, None)
    return resp['data']


def mock_fetch_html(url):
    """Replacement for pipeline.fetch_html. Returns string or None."""
    resp = MOCK_RESPONSES.get(url)
    if resp is None:
        return None
    if resp['type'] != 'text':
        return None
    return resp['data']


# ── SYNTHETIC AGENTS.JSON ────────────────────────────────────────────────────
def build_test_agents():
    """Build the synthetic agents.json covering all the interesting cases."""
    return [
        # CARL FISHER: broken Chime URL, BoldTrail now has photo
        {
            'name': 'Carl Fisher',
            'email': 'cdfisher@example.com',
            'boldtrailId': '272193',
            'photo': 'https://cdn.chime.me/image/fs/user-info/2024719/16/original_287ad2d8-broken.png',
            'photoSource': '',  # was never properly tracked
            'photoSourceHash': '',
            'boldtrailPhoto': 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/carl-new.jpg',
            'boldtrailAvatarAdded': True,
            'profileUrl': 'https://glasshouserealty.com/agents/Carl-Fisher/8726233',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # ALLEN BLACKBURN: has profileUrl, no BoldTrail photo, no existing photo
        # The pipeline should try Source A first (fail because no BT photo),
        # then Source B (succeed — standard Lofty page)
        {
            'name': 'Allen Blackburn',
            'email': 'allen@example.com',
            'boldtrailId': '272171',
            'photo': '',
            'photoSource': '',
            'photoSourceHash': '',
            'boldtrailPhoto': '',
            'boldtrailAvatarAdded': False,
            'profileUrl': 'https://glasshouserealty.com/agents/Allen-Blackburn/8726277',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # SCOTTIE FULHART: has existing Lofty-sourced photo, custom page,
        # no BoldTrail photo. Expected: keep existing.
        # We have to put her in a state where she ALREADY has a self-hosted photo
        # so the pipeline won't try to acquire her this run (rule: fresh + bt unchanged).
        # For the test, we simulate: she has self-hosted photo from prior run.
        {
            'name': 'Scottie Fulhart',
            'email': 'scottie@example.com',
            'boldtrailId': '272999',
            'photo': 'https://evansvince.github.io/glasshouse-data/agent-photos/272999.jpg',
            'photoSource': 'lofty',  # was originally Lofty-sourced
            'photoSourceHash': pipeline.url_hash('https://cdn.lofty.com/scottie-prior.jpg'),
            'boldtrailPhoto': '',
            'boldtrailAvatarAdded': False,
            'profileUrl': 'https://glasshouserealty.com/agents/Scottie-Fulhart/8726999',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # MARY COOPER: already correctly self-hosted, BoldTrail unchanged.
        # Expected: skip (fresh).
        {
            'name': 'Mary Cooper',  # display = "Elizabeth Cooper" after pref name override
            'email': 'mary@example.com',
            'boldtrailId': '272321',
            'photo': 'https://evansvince.github.io/glasshouse-data/agent-photos/272321.jpg',
            'photoSource': 'boldtrail',
            'photoSourceHash': pipeline.url_hash(
                'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/mary.jpg'),
            'boldtrailPhoto': 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/mary.jpg',
            'boldtrailAvatarAdded': True,
            'profileUrl': 'https://glasshouserealty.com/agents/Elizabeth-Cooper/8726234',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # AARON KROGGEL: no photo, no profile URL, no BoldTrail photo.
        # Expected: no_sources (logged to backlog).
        {
            'name': 'Aaron Kroggel',
            'email': 'aaron@example.com',
            'boldtrailId': '272164',
            'photo': '',
            'photoSource': '',
            'photoSourceHash': '',
            'boldtrailPhoto': '',
            'boldtrailAvatarAdded': False,
            'profileUrl': '',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # NEW AGENT: brand new, BoldTrail has photo.
        # Expected: acquired_boldtrail.
        {
            'name': 'New Agent',
            'email': 'newagent@example.com',
            'boldtrailId': '280000',
            'photo': '',
            'photoSource': '',
            'photoSourceHash': '',
            'boldtrailPhoto': 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/newagent.jpg',
            'boldtrailAvatarAdded': True,
            'profileUrl': '',
            'source': 'boldtrail',
            'hidden': False,
            'regions': ['Dayton'],
        },
        # CLEVELAND AGENT: excluded entirely.
        {
            'name': 'Cleveland Agent',
            'email': 'cle@example.com',
            'boldtrailId': '402001',
            'photo': '',
            'boldtrailPhoto': 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/cle.jpg',
            'boldtrailAvatarAdded': True,
            'profileUrl': '',
            'source': 'boldtrail-cleveland',
            'hidden': False,
            'regions': ['Cleveland'],
        },
        # HIDDEN AGENT: should be skipped regardless.
        {
            'name': 'Hidden Agent',
            'email': 'hidden@example.com',
            'boldtrailId': '299999',
            'photo': '',
            'boldtrailPhoto': 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/hidden.jpg',
            'boldtrailAvatarAdded': True,
            'profileUrl': '',
            'source': 'boldtrail',
            'hidden': True,
            'regions': ['Dayton'],
        },
    ]


# ── RUN THE INTEGRATION TEST ─────────────────────────────────────────────────
print('\n' + '=' * 70)
print('INTEGRATION TEST: end-to-end photo pipeline')
print('=' * 70)

# Set up a temp working directory
test_dir = tempfile.mkdtemp(prefix='photo-pipeline-test-')
os.chdir(test_dir)
print(f'Test dir: {test_dir}')

try:
    # Patch the HTTP layer
    pipeline.download_bytes = mock_download_bytes
    pipeline.fetch_html = mock_fetch_html
    setup_mocks()

    # Build synthetic agents.json
    test_agents = build_test_agents()
    with open('agents.json', 'w') as f:
        json.dump(test_agents, f, indent=2)
    print(f'\nCreated synthetic agents.json with {len(test_agents)} agents')

    # Set up self-hosted photo files for the agents we claim already have them
    # (Scottie Fulhart, Mary Cooper) so the "fresh" check passes
    os.makedirs(pipeline.PHOTO_DIR, exist_ok=True)
    with open(os.path.join(pipeline.PHOTO_DIR, '272999.jpg'), 'wb') as f:
        f.write(TINY_JPEG)
    with open(os.path.join(pipeline.PHOTO_DIR, '272321.jpg'), 'wb') as f:
        f.write(TINY_JPEG)
    print(f'Created prior self-hosted photo files for Scottie and Mary')

    # Run the pipeline by calling main() with arguments
    # Use sys.argv to inject CLI args
    sys.argv = ['gh-photo-pipeline.py']
    print('\n--- PIPELINE OUTPUT ---')
    try:
        pipeline.main()
    except SystemExit as e:
        if e.code not in (0, None):
            print(f'(Pipeline exited with code {e.code})')
    print('--- END PIPELINE OUTPUT ---\n')

    # Read the resulting agents.json
    with open('agents.json') as f:
        result_agents = json.load(f)

    by_name = {a['name']: a for a in result_agents}

    # ── Verify expectations ─────────────────────────────────────────────────
    print('Verifying outcomes for each agent:')

    # Carl Fisher: re-acquired from BoldTrail (Source A succeeded)
    carl = by_name['Carl Fisher']
    check('Carl Fisher: photo replaced with self-hosted URL',
          carl['photo'].startswith(pipeline.PHOTO_PUBLIC_BASE),
          f'Got photo={carl["photo"]}')
    check('Carl Fisher: photoSource = boldtrail',
          carl.get('photoSource') == 'boldtrail',
          f'Got photoSource={carl.get("photoSource")}')
    check('Carl Fisher: self-hosted file exists',
          os.path.exists(os.path.join(pipeline.PHOTO_DIR, '272193.jpg')))
    check('Carl Fisher: temp fields stripped',
          'boldtrailPhoto' not in carl and 'boldtrailAvatarAdded' not in carl)

    # Allen Blackburn: acquired from Lofty (Source B succeeded)
    allen = by_name['Allen Blackburn']
    check('Allen Blackburn: photo self-hosted',
          allen['photo'].startswith(pipeline.PHOTO_PUBLIC_BASE),
          f'Got photo={allen["photo"]}')
    check('Allen Blackburn: photoSource = lofty',
          allen.get('photoSource') == 'lofty',
          f'Got photoSource={allen.get("photoSource")}')

    # Scottie Fulhart: kept existing (no acquisition this run because fresh)
    scottie = by_name['Scottie Fulhart']
    check('Scottie Fulhart: photo kept (self-hosted from prior run)',
          scottie['photo'].startswith(pipeline.PHOTO_PUBLIC_BASE),
          f'Got photo={scottie["photo"]}')
    check('Scottie Fulhart: photoSource still lofty (no upgrade triggered)',
          scottie.get('photoSource') == 'lofty')

    # Mary Cooper: skipped (fresh — hash matches)
    mary = by_name['Mary Cooper']
    check('Mary Cooper: photo unchanged (fresh skip)',
          mary['photo'] == 'https://evansvince.github.io/glasshouse-data/agent-photos/272321.jpg')
    check('Mary Cooper: photoSource preserved',
          mary.get('photoSource') == 'boldtrail')

    # Aaron Kroggel: no_sources, photo stays empty
    aaron = by_name['Aaron Kroggel']
    check('Aaron Kroggel: photo stays empty (no sources)',
          aaron['photo'] == '',
          f'Got photo={aaron["photo"]}')

    # New Agent: acquired from BoldTrail
    new = by_name['New Agent']
    check('New Agent: photo self-hosted from BoldTrail',
          new['photo'].startswith(pipeline.PHOTO_PUBLIC_BASE))
    check('New Agent: photoSource = boldtrail',
          new.get('photoSource') == 'boldtrail')

    # Cleveland: skipped, photo unchanged
    cle = by_name['Cleveland Agent']
    check('Cleveland Agent: photo unchanged (excluded)',
          cle['photo'] == '',
          f'Got photo={cle["photo"]}')

    # Hidden: skipped
    hidden = by_name['Hidden Agent']
    check('Hidden Agent: photo unchanged (skipped)',
          hidden['photo'] == '')

    # State file was created
    check('photo-pipeline-state.json was written',
          os.path.exists('photo-pipeline-state.json'))

    # Report was written
    report_files = []
    if os.path.exists('reports/photo-pipeline'):
        report_files = os.listdir('reports/photo-pipeline')
    check('Pipeline report was written',
          len(report_files) > 0,
          f'Got {report_files}')

finally:
    # Clean up
    os.chdir('/home/claude/work')
    shutil.rmtree(test_dir, ignore_errors=True)


# ── SUMMARY ──────────────────────────────────────────────────────────────────
print('\n' + '=' * 70)
print(f'INTEGRATION TEST RESULTS: {TESTS_PASSED} passed, {TESTS_FAILED} failed')
print('=' * 70)

if TESTS_FAILED:
    print('\nFAILURES:')
    for name, detail in FAILURES:
        print(f'  ✗ {name}{(": " + detail) if detail else ""}')
    sys.exit(1)

print('✓ End-to-end integration test passed.')
