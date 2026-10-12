"""One-time import of operator-reviewed CTF discoveries from an owner-protected JSON file.

Never put real credential values in Git, command-line arguments or model prompts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend.campaign_service import CampaignService
from backend.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Import operator-reviewed persistent campaign context")
    parser.add_argument("import_file", type=Path, help="Local 0600 JSON file (not a Git-tracked file)")
    parser.add_argument("--campaign-id", default="", help="Stable campaign identity")
    parser.add_argument("--challenge", default="manual-import", help="Source challenge label")
    args = parser.parse_args()

    path = args.import_file.expanduser().resolve()
    if not path.is_file():
        parser.error("import file does not exist")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        parser.error("import file must be owner-only. Run chmod 600 on the input.")
    if path.stat().st_size > 2_000_000:
        parser.error("import file exceeds the 2 MiB safety limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        parser.error("import file must contain a JSON object")
    settings = Settings()
    if args.campaign_id:
        settings.campaign_id = args.campaign_id
    service = CampaignService(settings)
    count = service.store.publish(data, args.challenge)
    # Only report counts, never plaintext secrets.
    print(f"Campaign: {service.campaign_id}; imported {count['facts']} facts and "
          f"{count['credentials']} credential updates")
    print("Delete the plaintext import file after verifying campaign_search.")


if __name__ == "__main__":
    main()
