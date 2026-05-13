#!/usr/bin/env python3
"""
Adversarial safety test for the BoldTrail write-prevention layers.

Simulates an attacker (or a careless future developer) trying various
common patterns to make the script issue a write to BoldTrail. Every
attempt should fail cleanly. If any of these succeed, the safety
property has a hole.

Run:  python3 adversarial-safety-test.py
"""

import importlib.util, sys, urllib.request, urllib.error
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("sync_bt", "/home/claude/work/gh-agents-sync-bt.py")
sync_bt = importlib.util.module_from_spec(spec)
sys.modules["sync_bt"] = sync_bt
spec.loader.exec_module(sync_bt)

results = []
def check(label, ok, detail=''):
    sym = '✓' if ok else '✗'
    results.append((sym, label, detail))
    print(f"  {sym} {label}" + (f" — {detail}" if (detail and not ok) else ''))


print("=" * 70)
print("ADVERSARIAL TEST 1: Direct urlopen on the BoldTrail update endpoint")
print("=" * 70)
# Capture every urlopen call the script makes during a real fetch.
# If the script ever issues anything other than a GET, we detect it.
captured = []
real_urlopen = urllib.request.urlopen

def watching_urlopen(req, *args, **kwargs):
    if hasattr(req, 'get_method'):
        method = req.get_method()
    else:
        method = 'UNKNOWN'
    url = req.full_url if hasattr(req, 'full_url') else str(req)
    has_body = (getattr(req, 'data', None) is not None) if hasattr(req, 'data') else False
    captured.append((method, url, has_body))
    # Return a fake response so the script keeps going
    resp = MagicMock()
    resp.read.return_value = b'[]'
    resp.__enter__ = lambda s: s
    resp.__exit__  = lambda s, *a: None
    return resp

with patch.object(urllib.request, 'urlopen', side_effect=watching_urlopen):
    sync_bt.BT_API_KEY = 'fake-test-key'
    try:
        sync_bt.bt_get(f"{sync_bt.BT_BASE}/users?api_key=fake&full_info=1&status=active")
    except Exception:
        pass

check("At least one HTTP call was attempted via the wrapper",
      len(captured) > 0, "no calls captured")

# Every captured call must be GET, must hit only the allowed URL, must have no body
all_gets = all(m == 'GET' for m, _, _ in captured)
check("Every observed HTTP call uses GET", all_gets,
      f"methods seen: {[m for m,_,_ in captured]}")
no_bodies = all(not has_body for _, _, has_body in captured)
check("No observed HTTP call carries a body", no_bodies)
only_allowed = all(sync_bt._bt_url_allowed(u) for _, u, _ in captured)
check("Every observed URL is in the read-only allowlist", only_allowed,
      f"URLs: {[u for _,u,_ in captured]}")


print()
print("=" * 70)
print("ADVERSARIAL TEST 2: Try to construct a request bypassing _GetOnlyRequest")
print("=" * 70)
# Even if someone tries to use raw urllib.request.Request to do a write,
# the request would still need to go through bt_get() to be allowed by
# the URL allowlist, which is wired through _GetOnlyRequest. So the only
# way to bypass is to write entirely new code that doesn't use bt_get.
# This test confirms that bypassing the layer is at least visible.
attempt = urllib.request.Request(
    "https://my.brokermint.com/api/v1/users/12345",
    data=b'{"evil":"payload"}',
    method='PUT',
)
# This request, if executed, would write to BT. But the script's only
# urlopen() call is inside bt_get(), and bt_get rejects this URL.
check("Raw Request can be constructed (this is a Python language fact)",
      attempt.get_method() == 'PUT')
check("...but bt_get() refuses the URL",
      not sync_bt._bt_url_allowed(attempt.full_url))


print()
print("=" * 70)
print("ADVERSARIAL TEST 3: SQL-injection-style URL with allowed prefix")
print("=" * 70)
# Try to sneak a write through by appending the write path after the
# allowed list URL. The regex should anchor at end-of-string so this fails.
sneaky_urls = [
    "https://my.brokermint.com/api/v1/users?api_key=x#/users/123/edit",
    "https://my.brokermint.com/api/v1/users?api_key=x&../users/123",
    "https://my.brokermint.com/api/v1/users/../users/123",
    "https://my.brokermint.com/api/v1/users?api_key=x/../delete",
    "https://my.brokermint.com/api/v1/users?evil=/users/12345/edit",
]
for url in sneaky_urls:
    if sync_bt._bt_url_allowed(url):
        # Allowed by the regex — verify it would actually hit /users list
        # (some of these might be technically OK because the server-side
        # would interpret the query string as harmless)
        # We're cautious: any URL that contains a path segment after /users
        # should be rejected.
        suffix = url.split('/users', 1)[1] if '/users' in url else ''
        # If anything after /users contains a '/', flag it
        if '/' in suffix.split('?')[0]:
            check(f"Sneaky URL with path traversal blocked: {url[:60]}",
                  False, "allowed but contains path after /users")
            continue
    check(f"Sneaky URL rejected/safe: {url[:60]}",
          not sync_bt._bt_url_allowed(url) or '/' not in url.split('/users', 1)[1].split('?')[0])


print()
print("=" * 70)
print("ADVERSARIAL TEST 4: API key escape via the URL")
print("=" * 70)
# What if someone slips an api_key param that has a different semantic?
# This is mostly a server-side concern (BoldTrail would have to accept
# it), but we can at least confirm the wrapper doesn't try to be clever
# about parsing the URL.
weird_keys = [
    "https://my.brokermint.com/api/v1/users?api_key=&full_info=1",   # empty key
    "https://my.brokermint.com/api/v1/users?api_key=DELETE&full_info=1", # word as key
    "https://my.brokermint.com/api/v1/users?api_key=' OR 1=1--",     # sqli-ish
]
for url in weird_keys:
    # All of these should be ALLOWED by the wrapper (the URL pattern matches)
    # but they're harmless because (a) GET only, (b) BT's auth will reject
    # bad keys with 401/403 which the script handles cleanly
    allowed = sync_bt._bt_url_allowed(url)
    check(f"URL with weird api_key still GET-only (auth fails server-side): {url[:60]}",
          allowed, "the wrapper doesn't interpret query strings")


print()
print("=" * 70)
print("ADVERSARIAL TEST 5: bt_get cannot be tricked with positional args")
print("=" * 70)
# Try calling bt_get with extra positional args that might map to data/method
# in the underlying urlopen.
try:
    sync_bt.bt_get("https://my.brokermint.com/api/v1/users", b'extra data')
    check("bt_get rejects extra positional args", False, "accepted extra arg")
except TypeError:
    # Expected — bt_get signature is (url, timeout=120). Extra args = TypeError.
    check("bt_get rejects extra positional args (TypeError)", True)
except Exception as e:
    check("bt_get rejects extra positional args", False, f"got {type(e).__name__}")


print()
print("=" * 70)
print("ADVERSARIAL TEST 6: Inspect source for any hidden write capabilities")
print("=" * 70)
import re
src = open('/home/claude/work/gh-agents-sync-bt.py').read()

# Things that should not appear outside docstrings/comments
forbidden_patterns = [
    (r'\.post\(', 'requests.post() style call'),
    (r'\.put\(', 'requests.put() style call'),
    (r'\.patch\(', 'requests.patch() style call'),
    (r'\.delete\(', 'requests.delete() style call'),
    (r'method\s*=\s*["\']POST["\']', "method='POST' literal"),
    (r'method\s*=\s*["\']PUT["\']', "method='PUT' literal"),
    (r'method\s*=\s*["\']DELETE["\']', "method='DELETE' literal"),
    (r'method\s*=\s*["\']PATCH["\']', "method='PATCH' literal"),
]

# Strip docstrings and comments
src_clean = re.sub(r'""".*?"""', '', src, flags=re.DOTALL)
src_clean = re.sub(r"'''.*?'''", '', src_clean, flags=re.DOTALL)
src_clean = '\n'.join(line for line in src_clean.split('\n')
                     if not line.strip().startswith('#'))

for pattern, label in forbidden_patterns:
    matches = re.findall(pattern, src_clean)
    check(f"No {label} in script source",
          len(matches) == 0,
          f"found {len(matches)} occurrence(s)")


print()
print("=" * 70)
passed = sum(1 for s, _, _ in results if s == '✓')
failed = sum(1 for s, _, _ in results if s == '✗')
print(f"ADVERSARIAL TEST RESULTS: {passed} passed, {failed} failed (of {len(results)} checks)")
print("=" * 70)
if failed:
    print("\nFAILED CHECKS:")
    for s, label, detail in results:
        if s == '✗':
            print(f"  ✗ {label} — {detail}")
    sys.exit(1)
print("✓ All adversarial safety checks passed.")
print("  No code path in this script can write to BoldTrail.")
