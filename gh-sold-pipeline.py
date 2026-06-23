#!/usr/bin/env python3
"""
Glasshouse Realty — Sold Transactions Pipeline
Pulls 2026 closed transactions from BoldTrail V3 API,
geocodes addresses via Mapbox, outputs sold.json for GitHub Pages.

Feeds: homepage sold map + counter, Cleveland region map

READ ONLY: only GET requests are made to BoldTrail. Nothing upstream is
modified. Mapbox is called only to geocode addresses.

Usage:
    # Dayton account:
    python3 gh-sold-pipeline.py --key DAYTON_KEY --mapbox MAPBOX_TOKEN --out sold-dayton.json

    # Cleveland account:
    python3 gh-sold-pipeline.py --key CLEVELAND_KEY --mapbox MAPBOX_TOKEN --out sold-cleveland.json

    # Dry run (no geocoding, no file write):
    python3 gh-sold-pipeline.py --key DAYTON_KEY --mapbox MAPBOX_TOKEN --dry-run

    # Skip geocoding (just pull + filter, useful for testing):
    python3 gh-sold-pipeline.py --key DAYTON_KEY --mapbox MAPBOX_TOKEN --no-geocode

GEOCODE CACHE (added):
    Each successful geocode is remembered in a per-output cache file so the
    same address is never geocoded twice. The cache name is derived from --out
    (sold-dayton.json -> sold-dayton-geocache.json), so Dayton and Cleveland
    keep separate caches and never collide. After the first run, a refresh only
    geocodes genuinely new solds — a few calls instead of ~739. Commit the
    cache file alongside the sold file so the savings persist in CI.
    Override with --cache-file PATH, or disable with --no-cache.
"""

import json, time, argparse, re, sys, os
import urllib.request, urllib.parse, urllib.error
from datetime import datetime

BASE_URL    = 'https://my.brokermint.com/api/v3'
MAPBOX_BASE = 'https://api.mapbox.com/geocoding/v5/mapbox.places'
RATE_LIMIT  = 0.25   # seconds between API calls
GEO_LIMIT   = 0.12   # seconds between geocode calls (Mapbox allows ~600/min)

def bt_get(api_key, path, params=None):
    params = params or {}
    params['api_key'] = api_key
    qs = urllib.parse.urlencode(params, doseq=True)
    url = f"{BASE_URL}{path}?{qs}"
    try:
        req = urllib.request.urlopen(url, timeout=15)
        return json.loads(req.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} — {url}")
        return []
    except Exception as e:
        print(f"  Error: {e}")
        return []

# ── Geocode cache ─────────────────────────────────────────────────────────────
def cache_path_for(out_path, override):
    """Per-output cache filename: sold-dayton.json -> sold-dayton-geocache.json."""
    if override:
        return override
    if out_path:
        return re.sub(r'\.json$', '', out_path) + '-geocache.json'
    return 'geocode-cache.json'

def load_cache(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_cache(path, cache):
    if not path:
        return
    with open(path, 'w') as f:
        json.dump(cache, f, sort_keys=True, separators=(',', ':'))

def geocode(mapbox_token, address, city, state, zip_code, cache=None):
    """Geocode a full address via Mapbox. Returns (lat, lng) or (None, None).

    Uses `cache` (query -> [lat, lng]) when provided. Only SUCCESSFUL results
    are cached, so a temporary failure is retried on the next run rather than
    being remembered as permanently un-locatable.
    """
    query = f"{address}, {city}, {state} {zip_code}"
    if cache is not None and query in cache:
        lat, lng = cache[query]
        return lat, lng
    encoded = urllib.parse.quote(query)
    url = f"{MAPBOX_BASE}/{encoded}.json?access_token={mapbox_token}&country=US&limit=1"
    try:
        req = urllib.request.urlopen(url, timeout=10)
        data = json.loads(req.read().decode())
        features = data.get('features', [])
        if features:
            lng, lat = features[0]['geometry']['coordinates']
            lat, lng = round(lat, 6), round(lng, 6)
            if cache is not None:
                cache[query] = [lat, lng]
            return lat, lng
    except Exception as e:
        print(f"    Geocode error for '{query}': {e}")
    return None, None

def strip_street_number(address):
    """Remove the leading house number for privacy.
       '307 Kemper St'        -> 'Kemper St'
       '12B N Fountain Blvd'  -> 'N Fountain Blvd'   (letter-suffix handled)
       '12-14 Market St'      -> 'Market St'         (ranges handled)
       'Joellen Pl'           -> 'Joellen Pl'        (no leading number, unchanged)
    """
    if not address:
        return ''
    # leading house number: optional '#', digits, optional single letter,
    # optional range (-/– digits + optional letter), then required whitespace.
    return re.sub(r'^\s*#?\d+[A-Za-z]?(?:[-–/]\d+[A-Za-z]?)?\s+', '', address).strip()

def fmt_price(price):
    """Format price as $XXX,XXX"""
    return f"${int(price):,}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--key',      required=True, help='BoldTrail API key')
    parser.add_argument('--mapbox',   required=True, help='Mapbox access token')
    parser.add_argument('--out',      default='sold.json', help='Output file path')
    parser.add_argument('--dry-run',  action='store_true', help='Pull data only, no geocode, no file write')
    parser.add_argument('--no-geocode', action='store_true', help='Skip geocoding, output without coords')
    parser.add_argument('--year',     default='2026', help='Year to pull (default: 2026)')
    parser.add_argument('--cache-file', default=None, help='Geocode cache path (default: derived from --out)')
    parser.add_argument('--no-cache', action='store_true', help='Disable the geocode cache for this run')
    args = parser.parse_args()

    print(f"Glasshouse Sold Pipeline")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Output: {args.out}")
    print(f"Year:   {args.year}")
    print()

    # ── Step 1: Pull all closed transactions for the year ──────────────────
    print(f"Pulling closed transactions for {args.year}...")
    all_transactions = []
    last_id = None
    page = 1

    while True:
        params = {
            'count':              1000,
            'statuses':           'closed',
            'closing_date_range': 'ytd' if args.year == str(datetime.now().year) else f"{args.year}-01-01-{args.year}-12-31",
        }
        if last_id:
            params['starting_from_id'] = last_id

        print(f"  Page {page} (starting_from_id={last_id or 'beginning'})...", end=' ')
        batch = bt_get(args.key, '/transactions', params)

        if not batch:
            print("empty — done")
            break

        all_transactions.extend(batch)
        print(f"{len(batch)} records")

        if len(batch) < 1000:
            break  # Last page

        last_id = batch[-1]['id'] + 1
        page += 1
        time.sleep(RATE_LIMIT)

    print(f"\nTotal closed transactions pulled: {len(all_transactions)}")

    # ── Step 2: Filter to Residential only ────────────────────────────────
    residential = []
    for t in all_transactions:
        tt = (t.get('transaction_type') or '').lower()
        # Keep if type contains 'residential' or 'traditional' or 'single', or is blank
        if any(r in tt for r in ['residential', 'traditional', 'single']) or tt == '':
            residential.append(t)

    print(f"Residential only:               {len(residential)}")

    # ── Step 3: Filter to valid price + address ───────────────────────────
    valid = []
    for t in residential:
        price = t.get('price') or t.get('sales_volume') or 0
        address = (t.get('address') or '').strip()
        city    = (t.get('city') or '').strip()
        if price > 0 and address and city:
            valid.append(t)

    print(f"With valid address + price:     {len(valid)}")

    if args.dry_run:
        print(f"\nDRY RUN — sample output (first 5):")
        for t in valid[:5]:
            street = strip_street_number(t.get('address',''))
            price  = fmt_price(t.get('price') or t.get('sales_volume') or 0)
            city   = t.get('city','')
            print(f"  {street}, {city} | {price}")
        print(f"\nDry run complete — no geocoding, no file written")
        return

    # ── Step 4: Geocode (with persistent per-region cache) ─────────────────
    cache_path = None if args.no_cache else cache_path_for(args.out, args.cache_file)
    cache = {} if args.no_cache else load_cache(cache_path)
    if not args.no_geocode:
        print(f"\nGeocoding {len(valid)} addresses via Mapbox...")
        if cache_path:
            print(f"Cache: {cache_path} ({len(cache)} addresses already known)")
        print(f"(Rate limited to ~{int(1/GEO_LIMIT * 60)} requests/min)\n")

    sold = []
    geocoded = 0
    failed   = 0
    from_cache = 0

    for i, t in enumerate(valid):
        address  = t.get('address','').strip()
        city     = t.get('city','').strip()
        state    = t.get('state','OH')
        zip_code = t.get('zip','')
        price    = t.get('price') or t.get('sales_volume') or 0

        if args.no_geocode:
            lat, lng = None, None
        else:
            query = f"{address}, {city}, {state} {zip_code}"
            was_cached = (cache is not None and query in cache)
            lat, lng = geocode(args.mapbox, address, city, state, zip_code, cache)
            if lat and lng:
                geocoded += 1
                if was_cached:
                    from_cache += 1
                else:
                    # only a real Mapbox hit costs a request / needs the cooldown
                    time.sleep(GEO_LIMIT)
            else:
                failed += 1
            # lightweight progress every 100
            if (i + 1) % 100 == 0:
                print(f"  {i+1}/{len(valid)} processed ({from_cache} from cache)")

        sold.append({
            'street':  strip_street_number(address),
            'city':    city,
            'price':   int(price),
            'priceFmt': fmt_price(price),
            'lat':     lat,
            'lng':     lng,
            'closeDate': t.get('closing_date'),
        })

    # Persist the cache (new successful geocodes added during this run).
    if not args.no_cache and not args.no_geocode:
        save_cache(cache_path, cache)

    # Remove entries without coordinates (unless no-geocode mode)
    if not args.no_geocode:
        sold_with_coords = [s for s in sold if s['lat'] and s['lng']]
        new_calls = geocoded - from_cache
        print(f"\nGeocoded: {geocoded} ✓  ({from_cache} from cache, "
              f"{new_calls} new Mapbox calls)  Failed: {failed} ✗")
        print(f"Final records with coordinates: {len(sold_with_coords)}")
        sold = sold_with_coords

    # ── Step 5: Write output ──────────────────────────────────────────────
    with open(args.out, 'w') as f:
        json.dump(sold, f, separators=(',', ':'))

    print(f"\nWrote {len(sold)} records to {args.out}")
    print(f"File size: {len(json.dumps(sold)):,} bytes")
    if not args.no_cache and not args.no_geocode:
        print(f"Cache saved: {cache_path} ({len(cache)} addresses)")
    print(f"\nNext steps:")
    print(f"  cp {args.out} ~/glasshouse-data/{args.out}")
    print(f"  cd ~/glasshouse-data && git add {args.out} && git commit -m 'Update sold data' && git push")

if __name__ == '__main__':
    main()
