#!/usr/bin/env python3
"""
TCG Collect — database initialization entrypoint.

Creates **every** table and seeds baseline data (admin/default users, sample
cards, card-show directory). This is the first thing to run on a fresh
deployment, before syncing the catalog.

Why this exists instead of `flask custom generate_data`: this project imports its
app with `from __init__ import app`, which the Flask CLI cannot resolve, so all
`flask <group> <command>` invocations fail with "No such command". Running this
file directly from the repo root works because Python puts the repo root on the
import path.

Importing `main` is deliberate — it pulls in every model, which is what registers
all tables on SQLAlchemy's metadata. Importing only a subset (as an earlier
version of sync_catalog.py did) silently creates a half-initialized database
where, for example, /api/shows and signup return 500 because their tables were
never created.

Usage (from the backend repo root):

    python init_db.py            # create all tables + seed
    python init_db.py --tables   # create tables only, no seed data
"""
import argparse
import sys

# Importing main registers every model and blueprint. Side effects (request
# logging, cleanup scheduler) are harmless for a one-shot script that exits.
import main
from __init__ import app, db


# The seed functions main.generate_data calls, in dependency order. We invoke
# these directly rather than main.generate_data.callback(), because that command
# is wrapped in Flask's @with_appcontext and raises "no active click context"
# when called outside the CLI.
SEED_FUNCTIONS = [
    'initUsers', 'initSections', 'initGroups', 'initChannels', 'initPosts',
    'initNestPosts', 'initVotes', 'initSavedLocations', 'initSubscriptions',
    'initCatalog', 'initCollection', 'initCardShows',
]


def seed():
    """
    Run every seed function, isolating failures the way generate_data does.

    Wrapped in an app context because not every init function pushes its own
    (initSubscriptions in particular assumes one is already active, since the
    Flask CLI provided it via @with_appcontext). Nesting is safe for the ones
    that do push their own.
    """
    with app.app_context():
        for name in SEED_FUNCTIONS:
            fn = getattr(main, name, None)
            if fn is None:
                print(f"  - {name}: not found, skipping")
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — one bad seed shouldn't abort the rest
                print(f"  - {name} failed: {e}")


def create_tables():
    with app.app_context():
        db.create_all()
        names = sorted(db.metadata.tables.keys())
        print(f"Created/verified {len(names)} tables:")
        for name in names:
            print(f"  - {name}")
        return names


def main_entry(argv=None):
    parser = argparse.ArgumentParser(
        description="Create all tables and seed baseline data for TCG Collect.")
    parser.add_argument('--tables', action='store_true',
                        help='Create tables only; skip seeding')
    args = parser.parse_args(argv)

    try:
        create_tables()

        if not args.tables:
            print("\nSeeding baseline data…")
            seed()
    except Exception as e:  # noqa: BLE001 — clean non-zero exit for CI/cron
        print(f"Initialization failed: {e}", file=sys.stderr)
        return 1

    print("\nDatabase ready. Next: python sync_catalog.py --all")
    return 0


if __name__ == '__main__':
    sys.exit(main_entry())
