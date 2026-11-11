#!/usr/bin/env python3
"""Collect a small attributed economics library from the public Wikimedia API."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plagiarism import source_packs


def main():
    parser = argparse.ArgumentParser(description="Собрать открытые русскоязычные источники по экономике")
    parser.add_argument("--output", type=Path, default=source_packs.PACK_DIRECTORY / (source_packs.PACK_ID + ".json"))
    args = parser.parse_args()

    def progress(event, done, total):
        outcome = f"{event['characters']} символов" if event["status"] == "collected" else event["error"]
        print(f"[{done}/{total}] {event['title']} — {outcome}", file=sys.stderr, flush=True)

    try:
        pack = source_packs.collect_pack(on_progress=progress)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Не удалось собрать источники — {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"path": str(args.output.resolve()), "source_count": pack["source_count"],
                      "requested_source_count": pack["requested_source_count"], "omitted": pack["omitted"],
                      "total_characters": sum(len(source["text"]) for source in pack["sources"]),
                      "license": source_packs.LICENSE_NAME}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
