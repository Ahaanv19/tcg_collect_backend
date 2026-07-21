#!/usr/bin/env python3

""" db_migrate.py
Generates the database schema for all db models
- Initializes Users, Sections, and UserSections tables.
- Imports data from the old database to the new database.

Usage: Run from the terminal as such:

Goto the scripts directory:
> cd scripts; ./db_migrate.py

Or run from the root of the project:
> scripts/db_migrate.py

General Process outline:
0. Warning to the user.
1. Old data extraction.  An API has been created in the old project ...
  - Extract Data: retrieves data from the specified tables in the old database.
  - Transform Data: the API to JSON format understood by the new project.
2. New schema.  The schema is created in "this" new database.
3. Load Data: The bulk load API in "this" project inserts the data using required business logic.

"""
import shutil
import sys
import os

# Add the directory containing main.py to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Import application object.
#
# NOTE: `generate_data` is deliberately NOT imported. It is a click Command, so
# calling it as a plain function puts click into standalone mode, where it parses
# sys.argv and calls sys.exit() -- which previously meant this script could drop
# every table and then exit before seeding anything. We use init_db.seed()
# instead, which invokes the underlying init functions directly.
from main import app, db
from init_db import seed


def backup_database(db_uri, backup_uri):
    """
    Back up the current database.

    Returns True when a real backup was written, False otherwise. The caller
    must treat False as "there is no safety net" -- this used to print a
    reassuring-sounding message and then drop the database anyway.
    """
    if backup_uri:
        db_path = db_uri.replace('sqlite:///', 'instance/')
        backup_path = backup_uri.replace('sqlite:///', 'instance/')
        shutil.copyfile(db_path, backup_path)
        print(f"Database backed up to {backup_path}")
        return True

    print("!! No automatic backup: this is a non-SQLite (production) database.")
    print("!! Take a manual dump before continuing, e.g.:")
    print("!!   mysqldump -h <host> -u <user> -p <dbname> > backup.sql")
    return False

# Main extraction and loading process
def main():
    # `--yes` skips the prompt for non-interactive use. Without it, a run with no
    # attached TTY (e.g. `docker compose exec` without -it) would block forever on
    # input(), so we abort with an explanation instead of hanging.
    assume_yes = '--yes' in sys.argv or '-y' in sys.argv

    # Step 0: Warning to the user and backup table
    with app.app_context():
        try:
            # Check if the database has any tables
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()

            if tables:
                print(f"WARNING: this DROPS all {len(tables)} tables and every row in them.")
                print("If this database holds a synced card catalog, you will need to")
                print("re-run `python sync_catalog.py --all` afterwards (10-20 minutes).")

                if not assume_yes:
                    if not sys.stdin.isatty():
                        print("\nRefusing to drop tables without confirmation.")
                        print("Re-run interactively (docker compose exec -it ...) "
                              "or pass --yes if you are certain.")
                        sys.exit(1)
                    print("\nDo you want to continue? (y/n)")
                    if input().strip().lower() != 'y':
                        print("Exiting without making changes.")
                        sys.exit(0)

            # Backup the old database. A False return means no safety net exists.
            backed_up = backup_database(app.config['SQLALCHEMY_DATABASE_URI'],
                                        app.config['SQLALCHEMY_BACKUP_URI'])
            if not backed_up and tables and not assume_yes:
                print("\nContinue WITHOUT a backup? (y/n)")
                if input().strip().lower() != 'y':
                    print("Exiting without making changes.")
                    sys.exit(0)

        except SystemExit:
            raise
        except Exception as e:
            print(f"An error occurred: {e}")
            sys.exit(1)

    # Step 1: Build New schema and create test data
    try:
        with app.app_context():
            # Drop all the tables defined in the project
            db.drop_all()
            print("All tables dropped.")

            # Recreate the schema, then seed. seed() calls the init_* functions
            # directly, so nothing here depends on click's CLI context.
            db.create_all()
            print(f"Created {len(db.metadata.tables)} tables. Seeding data…")
            seed()

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

    # Log success
    print("Database initialized!")
    print("If this database backs the card catalog, run: python sync_catalog.py --all")

if __name__ == "__main__":
    main()