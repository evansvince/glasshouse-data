#!/usr/bin/env python3
"""
Local scenario simulator for the test environment.

Lets you stage realistic test scenarios in agents-test.json so the next
sync run produces predictable events. Useful when you don't want to wait
for actual BoldTrail changes to verify the sync's behavior.

Scenarios:
  ghost-agent     — adds a fake agent to agents-test.json with a made-up
                    boldtrailId that BoldTrail will not return. The next
                    sync should soft-delete this agent.

  expired-ghost   — adds a fake agent already marked softDeletedAt > 30
                    days ago. The next sync should purge them.

  stale-photo     — picks a real agent and strips their photo. The next
                    sync should re-derive the photo from the priority
                    chain (existing → BT → none).

  diff            — prints a summary of agents-test.json vs the live
                    agents.json so you can see drift.

  reset           — re-seed agents-test.json from the live file (calls
                    seed-test-data.py logic inline). Wipes any scenarios.

Usage:
  python3 test-scenarios.py ghost-agent
  python3 test-scenarios.py expired-ghost
  python3 test-scenarios.py stale-photo "Alice Smith"
  python3 test-scenarios.py diff
  python3 test-scenarios.py reset
"""

import json, sys, os, urllib.request, random, string
from datetime import datetime, timedelta, timezone

TEST_FILE = 'agents-test.json'
LIVE_URL  = 'https://evansvince.github.io/glasshouse-data/agents.json'
SOFT_DELETE_GRACE_DAYS = 30


def load_test():
    if not os.path.exists(TEST_FILE):
        print(f"FATAL: {TEST_FILE} not found. Run 'python3 seed-test-data.py' first.")
        sys.exit(1)
    with open(TEST_FILE) as f:
        return json.load(f)


def save_test(data):
    with open(TEST_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'), ensure_ascii=False)
    print(f"✓ Wrote {TEST_FILE} ({os.path.getsize(TEST_FILE):,} bytes)")


def random_btid():
    """Return a btid in a range that BoldTrail will not assign."""
    return str(random.randint(900000000, 999999999))


def random_suffix(n=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


def cmd_ghost_agent():
    """Add a fake agent that BoldTrail will never return → expects soft-delete on next sync."""
    data = load_test()
    suffix = random_suffix()
    ghost = {
        'name':        f'Test Ghost {suffix}',
        'email':       f'test-ghost-{suffix}@example.invalid',
        'phone':       '(937) 000-0000',
        'title':       'REALTOR®',
        'team':        '',
        'teamLogo':    '',
        'regions':     ['Dayton'],
        'office':      'Dayton',
        'photo':       '',
        'profileUrl':  '',
        'boldtrailId': random_btid(),
        'loftyId':     '',
        'hidden':      False,
        'source':      'boldtrail',
    }
    data.append(ghost)
    save_test(data)
    print(f"\nAdded ghost agent: {ghost['name']} <{ghost['email']}> btid:{ghost['boldtrailId']}")
    print(f"\nExpected behavior on next sync:")
    print(f"  - This agent will be soft-deleted (hidden:true + softDeletedAt set)")
    print(f"  - The SYNC_EVENT marker file will be written")
    print(f"  - The email digest will list this agent as soft-deleted")


def cmd_expired_ghost():
    """Add a fake agent already past the 30-day grace period → expects purge on next sync."""
    data = load_test()
    suffix = random_suffix()
    past = (datetime.now(timezone.utc) - timedelta(days=SOFT_DELETE_GRACE_DAYS + 5)).isoformat()
    ghost = {
        'name':           f'Test Expired Ghost {suffix}',
        'email':          f'test-expired-{suffix}@example.invalid',
        'phone':          '(937) 000-0000',
        'title':          'REALTOR®',
        'team':           '',
        'teamLogo':       '',
        'regions':        ['Dayton'],
        'office':         'Dayton',
        'photo':          '',
        'profileUrl':     '',
        'boldtrailId':    random_btid(),
        'loftyId':        '',
        'hidden':         True,
        'softDeletedAt':  past,
        'source':         'boldtrail',
    }
    data.append(ghost)
    save_test(data)
    print(f"\nAdded expired ghost: {ghost['name']} (softDeletedAt {past})")
    print(f"\nExpected behavior on next sync:")
    print(f"  - This agent will be PURGED (removed from agents-test.json entirely)")
    print(f"  - The SYNC_EVENT marker file will list them under 'purges'")


def cmd_stale_photo(name):
    """Strip the photo from a specific agent. Next sync should re-derive it."""
    data = load_test()
    target = None
    for a in data:
        if a.get('name', '').lower() == name.lower():
            target = a
            break
    if not target:
        print(f"FATAL: no agent named '{name}' found in {TEST_FILE}")
        sys.exit(1)
    old_photo = target.get('photo', '')
    target['photo'] = ''
    save_test(data)
    print(f"\nStripped photo from {target['name']}")
    print(f"  Old photo: {old_photo or '(none)'}")
    print(f"\nExpected behavior on next sync:")
    print(f"  - If BoldTrail has a photo for them, it will be picked up")
    print(f"  - Otherwise the photo stays empty until Lofty is set up")


def cmd_diff():
    """Compare agents-test.json against the live agents.json."""
    data = load_test()
    print(f"Fetching live data from {LIVE_URL} ...")
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=30) as r:
            live = json.loads(r.read().decode())
    except Exception as e:
        print(f"Could not fetch live: {e}")
        return

    test_by_email = {a.get('email', '').lower(): a for a in data if a.get('email')}
    live_by_email = {a.get('email', '').lower(): a for a in live if a.get('email')}

    only_in_test = set(test_by_email) - set(live_by_email)
    only_in_live = set(live_by_email) - set(test_by_email)
    in_both      = set(test_by_email) & set(live_by_email)

    print(f"\n── Diff: agents-test.json vs live agents.json ───────────")
    print(f"  Total in test:   {len(data)}")
    print(f"  Total in live:   {len(live)}")
    print(f"  Only in test:    {len(only_in_test)}")
    print(f"  Only in live:    {len(only_in_live)}")
    print(f"  In both:         {len(in_both)}")

    if only_in_test:
        print(f"\n  Agents only in test (likely staged scenarios):")
        for email in sorted(only_in_test)[:10]:
            a = test_by_email[email]
            sd = ' (soft-deleted)' if a.get('softDeletedAt') else ''
            h  = ' [hidden]' if a.get('hidden') else ''
            print(f"    + {a.get('name', '?')} <{email}>{h}{sd}")
        if len(only_in_test) > 10:
            print(f"    ... and {len(only_in_test) - 10} more")

    if only_in_live:
        print(f"\n  Agents only in live (will appear in test on next sync):")
        for email in sorted(only_in_live)[:10]:
            a = live_by_email[email]
            print(f"    - {a.get('name', '?')} <{email}>")
        if len(only_in_live) > 10:
            print(f"    ... and {len(only_in_live) - 10} more")


def cmd_reset():
    """Re-seed agents-test.json from the live data."""
    print(f"Fetching {LIVE_URL} ...")
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print(f"FATAL: could not fetch live: {e}")
        sys.exit(1)
    save_test(data)
    print(f"\nReset complete. {len(data)} records in {TEST_FILE}.")


def usage():
    print(__doc__)
    sys.exit(1)


def main():
    if len(sys.argv) < 2:
        usage()
    cmd = sys.argv[1]
    if cmd == 'ghost-agent':
        cmd_ghost_agent()
    elif cmd == 'expired-ghost':
        cmd_expired_ghost()
    elif cmd == 'stale-photo':
        if len(sys.argv) < 3:
            print("Usage: python3 test-scenarios.py stale-photo \"Agent Name\"")
            sys.exit(1)
        cmd_stale_photo(sys.argv[2])
    elif cmd == 'diff':
        cmd_diff()
    elif cmd == 'reset':
        cmd_reset()
    else:
        print(f"Unknown command: {cmd}")
        usage()


if __name__ == '__main__':
    main()
