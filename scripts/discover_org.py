#!/usr/bin/env python3
"""Discover the user's claude.ai organization UUID.

Reads the sessionKey from a file (path via --cookie-file or COOKIE_FILE env),
calls https://claude.ai/api/organizations, and prints one UUID per line on
stdout. install.sh consumes the first line; a multi-org user can pick from
the list.

Exits 0 on success, 1 on auth failure, 2 on no orgs returned.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", default=os.environ.get("COOKIE_FILE", ""))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not args.cookie_file:
        print("--cookie-file or COOKIE_FILE env required", file=sys.stderr)
        return 1
    cookie = Path(args.cookie_file).read_text().strip()
    if not cookie:
        print("cookie file is empty", file=sys.stderr)
        return 1

    req = urllib.request.Request(
        "https://claude.ai/api/organizations",
        headers={
            "Cookie": f"sessionKey={cookie}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            orgs = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from /api/organizations", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"network error: {e}", file=sys.stderr)
        return 1

    if not orgs:
        print("no organizations returned", file=sys.stderr)
        return 2

    for org in orgs:
        uuid = org.get("uuid") or org.get("id")
        name = org.get("name", "")
        if args.verbose:
            print(f"{uuid}\t{name}", file=sys.stderr)
        print(uuid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
