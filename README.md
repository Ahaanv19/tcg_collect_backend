# TCG Collect — Backend

Flask API and data layer for [TCG Collect](https://github.com/Ahaanv19/tcg_collect): a
Pokémon card collection tracker with cross-vendor price comparison and a card-show
directory.

## What it does

- **Catalog** — mirrors the [Pokémon TCG API](https://pokemontcg.io) (every set, every
  card, images) into local tables so browse pages are fast and price history accumulates.
- **Cross-vendor pricing** — stores TCGplayer (USD) and Cardmarket (EUR) prices side by
  side and ranks them cheapest-first, plus an eBay sold-listings comp link. A single
  marketplace will only ever show you its own price.
- **Collection** — owned cards with cost basis, condition, and grading, which turns a
  card list into a portfolio: unrealized gain/loss and set-completion percentage.
- **Want list + Show Mode** — the cards you're hunting, with your own max bid and the
  live market price next to each, so you can check a vendor's asking price at a booth.
- **Card shows** — searchable directory of expos (Collect-A-Con, regionals, local shows)
  by text, state, date window, or radius from a lat/lng.

## Tech stack

- Python 3 + Flask
- SQLAlchemy ORM (SQLite in development, MySQL in production)
- Flask-Login + JWT for auth
- Stripe for subscriptions

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # then set SECRET_KEY and POKEMON_TCG_API_KEY

flask custom generate_data   # create tables + seed sample data
python main.py               # serves on http://localhost:8288
```

## Syncing real card data

The seed data is four Base Set cards so a fresh clone renders something. To pull the
real catalog, get a free key at <https://dev.pokemontcg.io> and set
`POKEMON_TCG_API_KEY` in `.env`:

```bash
flask tcg sync-sets                  # set list only (fast)
flask tcg sync-cards --set-id base1  # one set
flask tcg sync-all --limit-sets 5    # smoke test
flask tcg sync-all                   # full catalog (~20k cards, slow)
flask tcg refresh-prices             # re-pull prices, record daily snapshots
```

Run `refresh-prices` on a daily schedule to build the price history that powers
portfolio charts and the `/api/movers` endpoint.

## API

Auth (`/api/auth`, `/api/users`):

| Method | Route | Notes |
|---|---|---|
| POST | `/api/auth/register` | email + password |
| POST | `/api/auth/login` | returns JWT, also sets httpOnly cookie |
| POST | `/api/authenticate` | uid + password (used by the web frontend) |
| GET | `/api/users/me` | current profile |

Catalog (public):

| Method | Route | Notes |
|---|---|---|
| GET | `/api/sets` | all sets, `?series=` |
| GET | `/api/sets/<set_id>/cards` | cards in a set |
| GET | `/api/cards` | search: `q`, `setId`, `rarity`, `type`, `minPrice`, `maxPrice`, `sort`, `page` |
| GET | `/api/cards/<card_id>` | card + buy options + 90d price history |
| GET | `/api/movers` | biggest price swings, `?days=&direction=` |
| GET | `/api/catalog/filters` | rarities/series/sets/types for filter menus |

Collection (auth required):

| Method | Route | Notes |
|---|---|---|
| GET/POST | `/api/collection` | list / add owned cards |
| PUT/DELETE | `/api/collection/<id>` | edit / remove a lot |
| GET | `/api/collection/summary` | value, cost basis, gain/loss |
| GET | `/api/collection/completion/<set_id>` | owned/total + missing cards + cost to complete |
| GET/POST | `/api/wantlist` | list / add wanted cards |
| GET | `/api/wantlist/budget?budget=200` | greedy show-budget planner |
| GET | `/api/show-mode` | want list + live prices + cheapest vendor |

Card shows (public read, Admin write):

| Method | Route | Notes |
|---|---|---|
| GET | `/api/shows` | `q`, `state`, `city`, `startAfter`, `startBefore`, `lat`+`lng`+`radius` |
| GET | `/api/shows/<id>` | one show |
| POST/PUT/DELETE | `/api/shows` | Admin only |

## Environment variables

```
SECRET_KEY=replace_with_secure_secret
POKEMON_TCG_API_KEY=your_key_from_dev.pokemontcg.io

# Production only — omit to use local SQLite
DB_ENDPOINT=
DB_USERNAME=
DB_PASSWORD=

STRIPE_SECRET_KEY=
```

## Backup / restore

```bash
flask custom backup_data    # writes backup/*.json
flask custom restore_data
```

The card catalog is deliberately **not** backed up — it is rebuildable at any time with
`flask tcg sync-all`. Only user-owned data (accounts, collection, want list) is captured.
Run a catalog sync before restoring, since collection rows referencing unknown cards are
skipped.
