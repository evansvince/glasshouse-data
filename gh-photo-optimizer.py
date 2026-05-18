#!/usr/bin/env python3
"""
Glasshouse Photo Optimizer
═══════════════════════════════════════════════════════════════════

Downloads BoldTrail-hosted agent headshots, resizes/compresses them,
and stores optimized versions in agent-photos/ to be served via
GitHub Pages.

Why we need this:
  - BoldTrail's S3 serves raw uploads with no resizing
  - Files are often 5MB+ with .gif extensions and octet-stream content-type
  - Result is slow page loads and inconsistent display quality

What this does:
  - Detects which agents have BoldTrail-sourced photos
  - Downloads each photo, auto-detects format from magic bytes
  - Resizes to fit within 800x800px (preserving aspect ratio)
  - Compresses to ~150KB JPEG quality target
  - Saves to agent-photos/{boldtrailId}.jpg
  - Updates the agent's photo URL in agents.json to point at the optimized version
  - Caches source URLs so unchanged photos are skipped on subsequent runs

Usage (called from main sync, or standalone):
  python3 gh-photo-optimizer.py            # process pending photos
  python3 gh-photo-optimizer.py --bulk     # force-process every BT photo
  python3 gh-photo-optimizer.py --dry-run  # preview, no changes
"""

import json, os, sys, argparse, hashlib, io
from datetime import datetime, timezone
import urllib.request, urllib.error

try:
    from PIL import Image, ImageOps
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow")
    sys.exit(1)

# ── CONFIG ───────────────────────────────────────────────────────────────────
AGENTS_FILE   = 'agents.json'
PHOTO_DIR     = 'agent-photos'
CACHE_FILE    = 'photo-cache.json'

# Output image targets
MAX_DIMENSION = 800   # pixels, longest edge
TARGET_KB     = 150   # approximate file size target

# Where BoldTrail photos are served from. Only optimize URLs matching this prefix.
BOLDTRAIL_S3_PREFIX = 'https://s3.us-west-2.amazonaws.com/brokermint.production/avatars/'

# Where optimized photos will be served from (relative to GitHub Pages root)
PHOTO_PUBLIC_BASE = 'https://evansvince.github.io/glasshouse-data/agent-photos'

# HTTP settings
DOWNLOAD_TIMEOUT = 30  # seconds per photo
USER_AGENT       = 'Glasshouse-Photo-Optimizer/1.0'


# ── CACHE ────────────────────────────────────────────────────────────────────
def load_cache():
    """Load source-url → optimized-path cache."""
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_cache(cache):
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2, sort_keys=True)


# ── DOWNLOAD ─────────────────────────────────────────────────────────────────
def download(url):
    """Download a URL, return bytes. Raises on failure."""
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as r:
        return r.read()


# ── OPTIMIZE ─────────────────────────────────────────────────────────────────
def optimize_bytes(raw_bytes):
    """
    Take raw image bytes (any format), return optimized JPEG bytes.

    Pillow detects the input format from magic bytes regardless of file
    extension or content-type. This is how we sidestep BoldTrail's
    octet-stream + .gif issues.
    """
    img = Image.open(io.BytesIO(raw_bytes))

    # Auto-correct EXIF rotation (some phones embed rotation as metadata
    # rather than rotating the pixels — PIL has a helper for this)
    img = ImageOps.exif_transpose(img)

    # Convert anything to RGB (handles PNG with alpha, RGBA modes, etc.)
    if img.mode != 'RGB':
        # White background for transparent images
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'RGBA':
            bg.paste(img, mask=img.split()[3])
        else:
            bg.paste(img)
        img = bg

    # Resize: fit within MAX_DIMENSION on the longest edge, preserve aspect
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)

    # Iteratively compress to hit the target size. JPEG quality 85 is
    # usually visually indistinguishable from 100, drops to ~70 if file
    # is still too big.
    for quality in (85, 80, 75, 70, 65):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
        if len(buf.getvalue()) <= TARGET_KB * 1024:
            return buf.getvalue()
    # Last attempt: lower quality but still acceptable
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=60, optimize=True, progressive=True)
    return buf.getvalue()


# ── PER-AGENT PROCESSING ─────────────────────────────────────────────────────
def needs_processing(agent, cache, bulk=False):
    """Return True if this agent's photo should be optimized this run."""
    photo = agent.get('photo', '')
    if not photo:
        return False
    # Only optimize BoldTrail-sourced photos (Lofty CDN photos are already good)
    if not photo.startswith(BOLDTRAIL_S3_PREFIX):
        return False
    # In bulk mode, process everything regardless of cache
    if bulk:
        return True
    # Check cache: if the exact source URL was already processed and the
    # output file still exists on disk, skip.
    btid = str(agent.get('boldtrailId', ''))
    cached = cache.get(photo)
    if cached and cached.get('boldtrailId') == btid:
        expected_path = os.path.join(PHOTO_DIR, f"{btid}.jpg")
        if os.path.exists(expected_path):
            return False
    return True


def process_agent(agent, cache):
    """
    Download, optimize, save. Returns the new public URL on success,
    None on failure (caller leaves the existing photo URL in place).
    """
    btid = str(agent.get('boldtrailId', ''))
    if not btid:
        return None
    source_url = agent.get('photo', '')
    if not source_url:
        return None

    try:
        raw = download(source_url)
    except urllib.error.HTTPError as e:
        print(f"  ✗ {agent['name']}: HTTP {e.code} downloading")
        return None
    except Exception as e:
        print(f"  ✗ {agent['name']}: download error: {e}")
        return None

    try:
        optimized = optimize_bytes(raw)
    except Exception as e:
        print(f"  ✗ {agent['name']}: optimization error: {e}")
        return None

    # Write to disk
    os.makedirs(PHOTO_DIR, exist_ok=True)
    out_path = os.path.join(PHOTO_DIR, f"{btid}.jpg")
    with open(out_path, 'wb') as f:
        f.write(optimized)

    # Update cache
    cache[source_url] = {
        'boldtrailId': btid,
        'optimized_size': len(optimized),
        'source_size': len(raw),
        'processed_at': datetime.now(timezone.utc).isoformat(),
        'output_path': out_path,
    }

    public_url = f"{PHOTO_PUBLIC_BASE}/{btid}.jpg"
    print(f"  ✓ {agent['name']}: {len(raw)//1024} KB → {len(optimized)//1024} KB")
    return public_url


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bulk', action='store_true',
                        help='Re-process all BoldTrail photos regardless of cache')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview what would be processed without making changes')
    parser.add_argument('--agents-file', default=AGENTS_FILE,
                        help=f'Path to agents JSON file (default: {AGENTS_FILE})')
    parser.add_argument('--limit', type=int, default=0,
                        help='Limit number of photos to process this run (0 = no limit)')
    args = parser.parse_args()

    print('=' * 60)
    print('Glasshouse Photo Optimizer')
    print(f'Timestamp: {datetime.now()}')
    print(f'Mode:      {("DRY RUN" if args.dry_run else "LIVE")}')
    print(f'Bulk:      {args.bulk}')
    print('=' * 60)

    if not os.path.exists(args.agents_file):
        print(f"ERROR: {args.agents_file} not found")
        sys.exit(1)

    with open(args.agents_file) as f:
        agents = json.load(f)
    print(f"Loaded {len(agents)} agents")

    cache = load_cache()
    print(f"Cache has {len(cache)} previously-processed URLs")

    # Find agents needing processing
    pending = [a for a in agents if needs_processing(a, cache, bulk=args.bulk)]
    print(f"\nAgents needing photo optimization: {len(pending)}")

    if args.limit and args.limit < len(pending):
        pending = pending[:args.limit]
        print(f"Limited to first {args.limit} this run")

    if not pending:
        print("Nothing to do.")
        sys.exit(0)

    if args.dry_run:
        print("\nWould process:")
        for a in pending[:20]:
            print(f"  - {a['name']} (btid:{a.get('boldtrailId')})")
        if len(pending) > 20:
            print(f"  ... and {len(pending) - 20} more")
        sys.exit(0)

    # Process
    print("\nProcessing...")
    updated_count = 0
    failed_count = 0
    for agent in pending:
        new_url = process_agent(agent, cache)
        if new_url:
            agent['photo'] = new_url
            updated_count += 1
        else:
            failed_count += 1

    save_cache(cache)

    # Write the updated agents.json back
    with open(args.agents_file, 'w', encoding='utf-8') as f:
        json.dump(agents, f, separators=(',', ':'), ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Processed: {updated_count} succeeded, {failed_count} failed")
    print(f"Updated {args.agents_file} with new photo URLs")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
