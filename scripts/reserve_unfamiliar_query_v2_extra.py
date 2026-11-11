#!/usr/bin/env python3
"""One separately authorized fixed extra publication, with unopened prose."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.reserve_unfamiliar_query_v2 import reserve

OUT = ROOT / "data/private/web-evaluation/query-unfamiliar-reserved-v2-extra"
URLS = (("RFC1149", "https://www.rfc-editor.org/rfc/rfc1149.txt"),)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch-fixed-extra", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(reserve(fetch=args.fetch_fixed_extra, directory=OUT, urls=URLS), ensure_ascii=False, indent=2))
