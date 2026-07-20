#!/usr/bin/env python3
"""
TCG Collect — catalog sync entrypoint.

A deploy-friendly wrapper around services/pokemontcg_service.py. The Flask `flask
tcg ...` CLI cannot be used here because this project imports its app with
`from __init__ import app`, which the Flask CLI can't resolve. Running this file
directly from the repo root sidesteps that: Python puts the repo root on the
import path, so `from __init__ import app` works.

Usage (run from the backend repo root, with the venv active):

    python sync_catalog.py --sets            # set list only (fast, ~1 min)
    python sync_catalog.py --set base1       # one set's cards
    python sync_catalog.py --all             # full catalog (~20k cards, 10-20 min)
    python sync_catalog.py --all --limit 5   # first 5 sets only (smoke test)
    python sync_catalog.py --refresh         # re-pull prices + daily snapshot (cron)

Requires POKEMON_TCG_API_KEY in the environment. `.env` is loaded automatically
by __init__.py, so on a server just make sure the key is in that file (or the
container's environment).

Exit code is 0 on success, 1 on failure, so it is safe to use in cron/CI.
"""
import argparse
import sys

# Importing __init__ initializes the Flask app, config, and db. It does NOT
# import main.py, so no blueprints, request logging, or cleanup scheduler start
# up — this stays a lightweight one-shot job.
from __init__ import app, db
from services.pokemontcg_service import sync_sets, sync_cards, sync_all, refresh_prices
# Imported so their tables are registered on db before create_all().
from model.tcg import Card, CardSet, PriceSnapshot  # noqa: F401


def build_parser():
    parser = argparse.ArgumentParser(
        description="Sync the Pokémon TCG catalog into this backend's database.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--all', action='store_true',
                        help='Sync every set and every card')
    action.add_argument('--sets', action='store_true',
                        help='Sync the set list only (fast)')
    action.add_argument('--set', metavar='SET_ID',
                        help='Sync one set by id, e.g. base1')
    action.add_argument('--refresh', action='store_true',
                        help='Re-pull prices for known sets and record a daily snapshot')
    parser.add_argument('--limit', type=int, default=None,
                        help='With --all: only sync the first N sets (smoke test)')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    with app.app_context():
        # Idempotent: safe whether or not `flask custom generate_data` has run.
        db.create_all()

        try:
            if args.all:
                sync_all(limit_sets=args.limit)
            elif args.sets:
                sync_sets()
            elif args.set:
                sync_cards(args.set)
            elif args.refresh:
                refresh_prices()
        except Exception as e:  # noqa: BLE001 — surface a clean non-zero exit for cron
            print(f"Sync failed: {e}", file=sys.stderr)
            return 1

    print("Done.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
