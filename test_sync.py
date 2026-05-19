#!/usr/bin/env python3
"""
Test harness for the patched gh-agents-sync-bt.py merge logic.
Runs in-process against mocked BoldTrail data — no network, no writes.
"""

import importlib.util, sys, json, os, copy
from datetime import datetime, timedelta, timezone

# Import the script as a module
spec = importlib.util.spec_from_file_location("sync_bt", "/home/claude/work/gh-agents-sync-bt.py")
sync_bt = importlib.util.module_from_spec(spec)
sys.modules["sync_bt"] = sync_bt
spec.loader.exec_module(sync_bt)


def reset_events():
    sync_bt.EVENTS['new_agents']   = []
    sync_bt.EVENTS['soft_deletes'] = []
    sync_bt.EVENTS['purges']       = []
    sync_bt.EVENTS['reactivated']  = []
    sync_bt.EVENTS['aborted']      = False
    sync_bt.EVENTS['abort_reason'] = ''


def make_bt_record(name, email, btid, photo='', team='', region='Dayton',
                   avatar_added=None):
    """
    Simulate what parse_bt() would produce.

    The avatar_added parameter mirrors BoldTrail's own signal for whether the
    agent has uploaded a real photo (vs. the default placeholder). If the
    test doesn't specify, we infer: avatar_added=True if a photo URL was
    provided, False otherwise. Most existing tests pre-date this field, so
    this default keeps them working without changes.
    """
    if avatar_added is None:
        avatar_added = bool(photo)
    return {
        'email':          email,
        'name':           name,
        'phone':          '(937) 555-0000',
        'title':          'REALTOR®',
        'team':           team,
        'teamLogo':       sync_bt.TEAM_LOGOS.get(team, ''),
        'regions':        [region],
        'office':         'Dayton',
        'photo':          '',
        'profileUrl':     '',
        'boldtrailId':    str(btid),
        'boldtrailPhoto': photo,
        'boldtrailAvatarAdded': avatar_added,
        'loftyId':        '',
        'source':         'boldtrail',
    }


def make_existing_record(name, email, btid, photo='', profile_url='', lofty_id='',
                          hidden=False, soft_deleted_at='', source='lofty',
                          region='Dayton', team='', team_logo=''):
    rec = {
        'name':        name,
        'email':       email,
        'phone':       '(937) 555-0000',
        'title':       'REALTOR®',
        'team':        team,
        'teamLogo':    team_logo,
        'regions':     [region],
        'office':      'Dayton',
        'photo':       photo,
        'profileUrl':  profile_url,
        'loftyId':     lofty_id,
        'boldtrailId': str(btid),
        'hidden':      hidden,
        'source':      source,
    }
    if soft_deleted_at:
        rec['softDeletedAt'] = soft_deleted_at
    return rec


def build_lookups(existing):
    by_email = {a['email'].lower(): a for a in existing if a.get('email')}
    by_btid  = {str(a['boldtrailId']): a for a in existing if a.get('boldtrailId')}
    by_name  = {}
    for a in existing:
        if a.get('name'):
            nk = sync_bt.normalize_name(a['name'])
            if nk: by_name.setdefault(nk, []).append(a)
    return by_email, by_btid, by_name


# Track results
results = []
def check(label, condition, detail=''):
    status = '✓' if condition else '✗'
    results.append((status, label, detail))
    print(f"  {status} {label}" + (f" — {detail}" if detail and not condition else ''))


print("=" * 70)
print("TEST 1: Steady state — same data in, same data out, zero events")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg",
                         profile_url="https://glasshouserealty.com/agents/Alice-Smith/8001",
                         lofty_id="8001"),
    make_existing_record("Bob Jones", "bob@gh.com", 101,
                         photo="https://cdn.lofty.com/bob.jpg",
                         profile_url="https://glasshouserealty.com/agents/Bob-Jones/8002",
                         lofty_id="8002"),
]
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
    make_bt_record("Bob Jones", "bob@gh.com", 101),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("All agents preserved", len(merged) == 2, f"got {len(merged)}")
check("Alice's Lofty photo preserved", merged[0]['photo'] == "https://cdn.lofty.com/alice.jpg",
      f"got {merged[0]['photo']}")
check("Alice's profileUrl preserved",
      merged[0]['profileUrl'] == "https://glasshouserealty.com/agents/Alice-Smith/8001")
check("Alice's loftyId preserved", merged[0]['loftyId'] == "8001")
check("Zero new agent events", len(sync_bt.EVENTS['new_agents']) == 0,
      f"got {len(sync_bt.EVENTS['new_agents'])}")
check("Zero soft-deletes", len(sync_bt.EVENTS['soft_deletes']) == 0)
check("boldtrailPhoto retained in merge output for pipeline consumption",
      'boldtrailPhoto' in merged[0],
      "Pipeline reads this field from agents.json to decide acquisition. "
      "Photo pipeline strips it when it writes agents.json.")


print()
print("=" * 70)
print("TEST 2: New agent appears in BoldTrail with a BT photo, no Lofty data")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg",
                         profile_url="https://glasshouserealty.com/agents/Alice-Smith/8001",
                         lofty_id="8001"),
]
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
    make_bt_record("Carol New", "carol@gh.com", 102,
                   photo="https://boldtrail.example/carol.jpg"),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Now 2 agents on site", len(merged) == 2)
carol = next((a for a in merged if a['email'] == 'carol@gh.com'), None)
check("Carol is present", carol is not None)
check("Carol uses BT photo (no Lofty exists yet)",
      carol and carol['photo'] == "https://boldtrail.example/carol.jpg",
      f"got {carol['photo'] if carol else 'None'}")
check("Carol is NOT hidden", carol and carol.get('hidden') == False)
check("Carol fires new-agent event", len(sync_bt.EVENTS['new_agents']) == 1)
check("Carol's event includes correct name",
      sync_bt.EVENTS['new_agents'][0]['name'] == 'Carol New')
check("Carol's event flags has_photo=True",
      sync_bt.EVENTS['new_agents'][0]['has_photo'] == True)


print()
print("=" * 70)
print("TEST 3: Lofty photo always beats BT photo (priority chain)")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice-pretty.jpg",
                         profile_url="https://glasshouserealty.com/agents/Alice-Smith/8001"),
]
bt = [
    # BT now reports a different (worse) photo for Alice
    make_bt_record("Alice Smith", "alice@gh.com", 100,
                   photo="https://boldtrail.example/alice-ugly.jpg"),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Alice keeps the Lofty photo", merged[0]['photo'] == "https://cdn.lofty.com/alice-pretty.jpg",
      f"got {merged[0]['photo']}")


print()
print("=" * 70)
print("TEST 4: Existing agent disappears from BoldTrail → soft-delete")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg"),
    make_existing_record("Bob Jones", "bob@gh.com", 101,
                         photo="https://cdn.lofty.com/bob.jpg"),
]
# Bob disappears from BT
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Both agents still in merged (Bob soft-deleted, not removed)", len(merged) == 2)
bob = next((a for a in merged if a['email'] == 'bob@gh.com'), None)
check("Bob is present", bob is not None)
check("Bob is hidden:true", bob and bob.get('hidden') == True)
check("Bob has softDeletedAt timestamp", bob and 'softDeletedAt' in bob)
check("Bob keeps his photo (preserved for potential reactivation)",
      bob and bob['photo'] == "https://cdn.lofty.com/bob.jpg")
check("Soft-delete event fired", len(sync_bt.EVENTS['soft_deletes']) == 1)
check("Soft-delete event names Bob", sync_bt.EVENTS['soft_deletes'][0]['name'] == 'Bob Jones')


print()
print("=" * 70)
print("TEST 5: Soft-deleted agent reappears within 30-day grace → reactivate")
print("=" * 70)
reset_events()
# Bob was soft-deleted 5 days ago
five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg"),
    make_existing_record("Bob Jones", "bob@gh.com", 101,
                         photo="https://cdn.lofty.com/bob.jpg",
                         hidden=True,
                         soft_deleted_at=five_days_ago),
]
# Bob is back in BT
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
    make_bt_record("Bob Jones", "bob@gh.com", 101),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
bob = next((a for a in merged if a['email'] == 'bob@gh.com'), None)
check("Bob is present", bob is not None)
check("Bob is no longer hidden", bob and bob.get('hidden') == False)
check("Bob's softDeletedAt is cleared",
      bob and not bob.get('softDeletedAt'),
      f"got {bob.get('softDeletedAt') if bob else 'None'}")
check("Reactivation event fired", len(sync_bt.EVENTS['reactivated']) == 1)


print()
print("=" * 70)
print("TEST 6: Soft-deleted agent past 30-day grace → purge")
print("=" * 70)
reset_events()
forty_days_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg"),
    make_existing_record("Bob Jones", "bob@gh.com", 101,
                         photo="https://cdn.lofty.com/bob.jpg",
                         hidden=True,
                         soft_deleted_at=forty_days_ago),
]
# Bob still gone from BT
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Only Alice remains", len(merged) == 1, f"got {len(merged)} agents")
check("Bob is purged", not any(a['email'] == 'bob@gh.com' for a in merged))
check("Purge event fired", len(sync_bt.EVENTS['purges']) == 1)
check("Purge event names Bob", sync_bt.EVENTS['purges'][0]['name'] == 'Bob Jones')


print()
print("=" * 70)
print("TEST 7: Cleveland agents pass through untouched")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg"),
    # Cleveland agent from spreadsheet
    make_existing_record("Cleveland Carl", "carl@asacox.com", 293000,
                         photo="https://lh3.googleusercontent.com/carl.jpg",
                         source="spreadsheet", region="Cleveland",
                         team_logo="https://evansvince.github.io/glasshouse-data/team-logos/ACH Logos White Background.png"),
]
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
    # BT doesn't know about Carl
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Both agents present", len(merged) == 2)
carl = next((a for a in merged if a['email'] == 'carl@asacox.com'), None)
check("Carl preserved", carl is not None)
check("Carl is NOT soft-deleted (Cleveland is read-only to this sync)",
      carl and not carl.get('softDeletedAt'))
check("Carl is NOT hidden", carl and carl.get('hidden') == False)
check("Carl's teamLogo preserved",
      carl and 'ACH Logos' in (carl.get('teamLogo') or ''))
check("Zero soft-deletes (Carl absence is expected for Cleveland)",
      len(sync_bt.EVENTS['soft_deletes']) == 0)


print()
print("=" * 70)
print("TEST 8: Manually hidden:true agent stays hidden across syncs")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg",
                         hidden=True),  # manually hidden
]
# Alice is still active in BT
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Alice still in merged", len(merged) == 1)
check("Alice REMAINS hidden:true despite being active in BT",
      merged[0].get('hidden') == True,
      f"got hidden={merged[0].get('hidden')}")
check("No reactivation event (she wasn't soft-deleted)",
      len(sync_bt.EVENTS['reactivated']) == 0)


print()
print("=" * 70)
print("TEST 9: BoldTrail tries to claim Cleveland → dropped at parse time")
print("=" * 70)
reset_events()
# Cleveland is now a first-class account. A Cleveland-account record gets
# regions:['Cleveland'] auto-assigned and passes through. A record from the
# Dayton account that happens to claim Cleveland also passes (no more legacy
# block). What matters is the parse_bt account parameter.

# Cleveland account: auto-region assignment
cleveland_rec = {
    'email': 'agent@asacoxhomes.com',
    'first_name': 'Heather',
    'last_name': 'Test',
    'role': 'Agent',
    'active': True,
    'id': 99999,
}
parsed = sync_bt.parse_bt(cleveland_rec, account='cleveland')
check("Cleveland account auto-assigns Cleveland region",
      parsed is not None and 'Cleveland' in parsed.get('regions', []))

# Dayton account: cleveland-claiming record does NOT get blocked anymore
dayton_with_cleveland_claim = {
    'email': 'weird@dayton.com',
    'first_name': 'Weird',
    'last_name': 'Case',
    'role': 'Agent',
    'active': True,
    'region': 'Cleveland',
    'id': 99998,
}
parsed = sync_bt.parse_bt(dayton_with_cleveland_claim, account='dayton')
check("Dayton account record claiming Cleveland passes through (no legacy block)",
      parsed is not None)


print()
print("=" * 70)
print("TEST 10: Name-match fallback (email changed in BT) preserves photo/profileUrl")
print("=" * 70)
reset_events()
# Existing record has old email
existing = [
    make_existing_record("Alice Smith", "alice.old@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg",
                         profile_url="https://glasshouserealty.com/agents/Alice-Smith/8001",
                         lofty_id="8001"),
]
# BT now reports Alice with a NEW email (and same btid)
bt = [
    make_bt_record("Alice Smith", "alice.new@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Alice matched (via btid)", len(merged) == 1)
check("Alice's photo preserved", merged[0]['photo'] == "https://cdn.lofty.com/alice.jpg")
check("Alice's profileUrl preserved",
      merged[0]['profileUrl'] == "https://glasshouserealty.com/agents/Alice-Smith/8001")
check("Alice's loftyId preserved", merged[0]['loftyId'] == "8001")
check("Alice's email updated to new", merged[0]['email'] == "alice.new@gh.com")
check("No new-agent event (she's not new)", len(sync_bt.EVENTS['new_agents']) == 0)


print()
print("=" * 70)
print("TEST 11: Name-match when BOTH email AND btid changed in BT")
print("=" * 70)
reset_events()
# Existing record has old email AND no btid
existing = [
    make_existing_record("Alice Smith", "alice.old@gh.com", "",
                         photo="https://cdn.lofty.com/alice.jpg",
                         profile_url="https://glasshouserealty.com/agents/Alice-Smith/8001",
                         lofty_id="8001"),
]
# Remove the boldtrailId entirely (simulates Lofty-only existing record from history)
existing[0].pop('boldtrailId', None)
# BT now reports Alice with new email AND assigns her a btid
bt = [
    make_bt_record("Alice Smith", "alice.new@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Alice matched via name", len(merged) == 1)
check("Alice's Lofty photo preserved",
      merged[0]['photo'] == "https://cdn.lofty.com/alice.jpg",
      f"got {merged[0]['photo']}")
check("Alice's loftyId preserved", merged[0]['loftyId'] == "8001")
check("Alice now has the new boldtrailId", merged[0]['boldtrailId'] == "100")


print()
print("=" * 70)
print("TEST 12: Ambiguous name (two agents with same name) refuses to name-match")
print("=" * 70)
reset_events()
existing = [
    make_existing_record("John Smith", "john.a@gh.com", "",
                         photo="https://cdn.lofty.com/john-a.jpg",
                         lofty_id="8001"),
    make_existing_record("John Smith", "john.b@gh.com", "",
                         photo="https://cdn.lofty.com/john-b.jpg",
                         lofty_id="8002"),
]
for e in existing: e.pop('boldtrailId', None)
# BT sends one John Smith with new email
bt = [
    make_bt_record("John Smith", "john.new@gh.com", 100),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
# Should NOT name-match (ambiguous) — treats as new
new_john = next((a for a in merged if a['email'] == 'john.new@gh.com'), None)
check("BT John Smith treated as new (no name match because ambiguous)",
      new_john and not new_john.get('photo'),
      f"got photo={new_john.get('photo') if new_john else 'None'}")
check("Two existing Johns get soft-deleted",
      len(sync_bt.EVENTS['soft_deletes']) == 2)


print()
print("=" * 70)
print("TEST 13: teamLogo — existing value wins over TEAM_LOGOS dict")
print("=" * 70)
reset_events()
custom_logo = "https://evansvince.github.io/glasshouse-data/team-logos/Custom_Override.png"
existing = [
    make_existing_record("Alice Smith", "alice@gh.com", 100,
                         photo="https://cdn.lofty.com/alice.jpg",
                         team="The Blair Team",
                         team_logo=custom_logo),
]
bt = [
    make_bt_record("Alice Smith", "alice@gh.com", 100, team="The Blair Team"),
]
by_email, by_btid, by_name = build_lookups(existing)
merged = sync_bt.merge(bt, by_email, by_btid, by_name, existing)
check("Custom teamLogo override is preserved",
      merged[0]['teamLogo'] == custom_logo,
      f"got {merged[0]['teamLogo']}")


print()
print("=" * 70)
print("TEST 14: BoldTrail safety — _GetOnlyRequest rejects 'data=' (body)")
print("=" * 70)
import urllib.request as _ur
try:
    sync_bt._GetOnlyRequest(
        "https://my.brokermint.com/api/v1/users",
        data=b'{"malicious":"payload"}',
    )
    check("data= body rejected by _GetOnlyRequest", False,
          "construction succeeded; should have raised RuntimeError")
except RuntimeError as e:
    check("data= body rejected by _GetOnlyRequest", True)
    check("Rejection message mentions read-only",
          'read-only' in str(e).lower() or 'body' in str(e).lower())


print()
print("=" * 70)
print("TEST 15: BoldTrail safety — _GetOnlyRequest.get_method() always returns GET")
print("=" * 70)
req = sync_bt._GetOnlyRequest("https://my.brokermint.com/api/v1/users")
check("get_method() returns 'GET' by default", req.get_method() == 'GET')

# Verify that the 'method=' kwarg is stripped/ignored
req2 = sync_bt._GetOnlyRequest("https://my.brokermint.com/api/v1/users", method='POST')
check("method='POST' kwarg is overridden — still returns 'GET'",
      req2.get_method() == 'GET',
      f"got {req2.get_method()}")
req3 = sync_bt._GetOnlyRequest("https://my.brokermint.com/api/v1/users", method='DELETE')
check("method='DELETE' kwarg is overridden — still returns 'GET'",
      req3.get_method() == 'GET',
      f"got {req3.get_method()}")
req4 = sync_bt._GetOnlyRequest("https://my.brokermint.com/api/v1/users", method='PUT')
check("method='PUT' kwarg is overridden — still returns 'GET'",
      req4.get_method() == 'GET',
      f"got {req4.get_method()}")


print()
print("=" * 70)
print("TEST 16: BoldTrail safety — URL allowlist rejects non-allowed endpoints")
print("=" * 70)
allowed   = "https://my.brokermint.com/api/v1/users?api_key=x&status=active"
blocked_1 = "https://my.brokermint.com/api/v1/users/12345"       # specific user
blocked_2 = "https://my.brokermint.com/api/v1/users/12345/edit"  # edit subpath
blocked_3 = "https://my.brokermint.com/api/v1/transactions"      # different resource
blocked_4 = "https://my.brokermint.com/api/v2/users"             # different version
blocked_5 = "https://attacker.example/api/v1/users"              # different host

check("Allowed: /users list endpoint",
      sync_bt._bt_url_allowed(allowed) == True)
check("Blocked: /users/{id} specific user",
      sync_bt._bt_url_allowed(blocked_1) == False)
check("Blocked: /users/{id}/edit subpath",
      sync_bt._bt_url_allowed(blocked_2) == False)
check("Blocked: /transactions different resource",
      sync_bt._bt_url_allowed(blocked_3) == False)
check("Blocked: /api/v2/users different API version",
      sync_bt._bt_url_allowed(blocked_4) == False)
check("Blocked: different hostname",
      sync_bt._bt_url_allowed(blocked_5) == False)


print()
print("=" * 70)
print("TEST 17: BoldTrail safety — bt_get() refuses non-allowed URLs at runtime")
print("=" * 70)
try:
    sync_bt.bt_get("https://my.brokermint.com/api/v1/users/12345")
    check("bt_get refuses non-allowed URL", False, "no exception raised")
except RuntimeError as e:
    check("bt_get refuses non-allowed URL", True)
    check("RuntimeError mentions safety violation",
          'safety' in str(e).lower() or 'allowlist' in str(e).lower())
except Exception as e:
    # If it raised something else (e.g., network error), that's a different
    # kind of failure — the safety check should have caught it FIRST.
    check("bt_get raises RuntimeError (not network error) for bad URL", False,
          f"got {type(e).__name__}: {e}")


print()
print("=" * 70)
print("TEST 18: BoldTrail safety — bt_get() function signature has no write params")
print("=" * 70)
import inspect
sig = inspect.signature(sync_bt.bt_get)
params = set(sig.parameters.keys())
check("bt_get has no 'data' parameter", 'data' not in params,
      f"got params: {params}")
check("bt_get has no 'method' parameter", 'method' not in params,
      f"got params: {params}")
check("bt_get has no 'body' parameter", 'body' not in params,
      f"got params: {params}")
check("bt_get has no 'json' parameter", 'json' not in params,
      f"got params: {params}")
check("bt_get accepts only url and timeout",
      params == {'url', 'timeout'},
      f"got params: {params}")


print()
print("=" * 70)
print("TEST 19: BoldTrail safety — single chokepoint for BoldTrail traffic")
print("=" * 70)
# Read the script source and count urlopen invocations by context.
# Goal: only ONE urlopen handles BoldTrail (the bt_get chokepoint).
# Other urlopen calls (e.g. profile-URL verifier) MUST use non-BoldTrail URLs.
import re as _re
src = open('/home/claude/work/gh-agents-sync-bt.py').read()
src_no_docstrings = _re.sub(r'""".*?"""', '', src, flags=_re.DOTALL)
src_no_docstrings = _re.sub(r"'''.*?'''", '', src_no_docstrings, flags=_re.DOTALL)
src_no_comments = '\n'.join(
    line for line in src_no_docstrings.split('\n')
    if not line.strip().startswith('#')
)
urlopen_count = src_no_comments.count('urllib.request.urlopen(')
check("urllib.request.urlopen() is used (at least once)",
      urlopen_count >= 1,
      f"found {urlopen_count} calls")
# The bt_get function must contain exactly one urlopen — that's the chokepoint
bt_get_match = _re.search(r'def bt_get\([^)]*\):.*?(?=\ndef |\Z)', src_no_comments, _re.DOTALL)
bt_get_body = bt_get_match.group(0) if bt_get_match else ''
check("bt_get() contains exactly one urlopen call",
      bt_get_body.count('urllib.request.urlopen(') == 1,
      f"found {bt_get_body.count('urllib.request.urlopen(')} in bt_get")
# Any urlopen calls outside bt_get must NOT pass BoldTrail URLs.
# We verify by ensuring no urlopen call references BT_BASE or my.brokermint.com
# anywhere except inside bt_get.
other_urlopen_chunks = src_no_comments.replace(bt_get_body, '')
# Look for BoldTrail markers near non-bt_get urlopen calls
non_bt_get_urlopen = other_urlopen_chunks.count('urllib.request.urlopen(')
if non_bt_get_urlopen > 0:
    # Check none of them reference brokermint
    suspect_chunks = []
    for line_idx, line in enumerate(other_urlopen_chunks.split('\n')):
        if 'urllib.request.urlopen(' in line:
            # Look at surrounding 5 lines
            lines = other_urlopen_chunks.split('\n')
            context = '\n'.join(lines[max(0, line_idx-5):line_idx+5])
            if 'brokermint' in context.lower() or 'BT_BASE' in context:
                suspect_chunks.append(context)
    check("Non-bt_get urlopen calls do not reference BoldTrail URLs",
          len(suspect_chunks) == 0,
          f"found {len(suspect_chunks)} suspect calls")

# Same check for requests library — we should NOT import it
import_lines = _re.findall(r'^\s*(?:import|from)\s+(\S+)', src, flags=_re.MULTILINE)
http_imports = [i for i in import_lines if 'requests' in i or 'httpx' in i or 'aiohttp' in i]
check("No requests/httpx/aiohttp imports (urllib only)",
      len(http_imports) == 0,
      f"found: {http_imports}")


print()
print("=" * 70)
print("TEST 20: BoldTrail safety — --safety-audit flag exits cleanly")
print("=" * 70)
# Just verify the function exists and runs without error
try:
    import io as _io, contextlib as _ctx
    buf = _io.StringIO()
    with _ctx.redirect_stdout(buf):
        sync_bt.print_safety_audit()
    audit_output = buf.getvalue()
    check("print_safety_audit() runs without error", True)
    check("Audit mentions all four layers",
          all(f"Layer {n}" in audit_output for n in range(1, 5)),
          f"output: {audit_output[:200]}")
    check("Audit lists the URL allowlist",
          'BT_READ_ALLOWLIST' in audit_output or 'allowlist' in audit_output.lower())
except Exception as e:
    check("print_safety_audit() runs without error", False, str(e))


# ── Summary ──────────────────────────────────────────────────────────────────
print()
print("=" * 70)
passed = sum(1 for s, _, _ in results if s == '✓')
failed = sum(1 for s, _, _ in results if s == '✗')
print(f"RESULTS: {passed} passed, {failed} failed (of {len(results)} checks)")
print("=" * 70)
if failed:
    print("\nFAILED CHECKS:")
    for s, label, detail in results:
        if s == '✗':
            print(f"  ✗ {label} — {detail}")
    sys.exit(1)
print("✓ All tests passed.")
