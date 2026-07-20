"""
TCG Collect — Pokémon TCG API sync service

Pulls set and card data from https://pokemontcg.io (API v2) into local tables so
the app serves its own catalog. Upstream is rate limited and occasionally slow;
mirroring it means browse pages stay fast and price history accumulates over
time instead of only ever showing "now".

Set POKEMON_TCG_API_KEY in .env. Without a key the API still works but at a much
lower rate limit, which is fine for development.

Usage (Flask CLI, registered in main.py):
    flask tcg sync-sets
    flask tcg sync-cards --set-id base1
    flask tcg sync-all
    flask tcg refresh-prices
"""
import os
import time
from datetime import datetime

import requests

from __init__ import app, db
from model.tcg import CardSet, Card, PriceSnapshot

API_BASE = 'https://api.pokemontcg.io/v2'
PAGE_SIZE = 250  # upstream maximum
REQUEST_TIMEOUT = 60  # upstream can be slow on full-page responses
MAX_ATTEMPTS = 3

# TCGplayer reports prices per printing. When a card has several we prefer the
# variant collectors actually chase, falling back down the list.
TCGPLAYER_VARIANT_PRIORITY = [
    '1stEditionHolofoil',
    'holofoil',
    '1stEditionNormal',
    'reverseHolofoil',
    'normal',
    'unlimitedHolofoil',
]


def _headers():
    key = os.environ.get('POKEMON_TCG_API_KEY')
    return {'X-Api-Key': key} if key else {}


def _get(path, params=None):
    """
    GET against the upstream API with retries on both transient HTTP errors
    (429/5xx) and network-level timeouts.

    pokemontcg.io is frequently slow to respond, so a timeout is expected noise
    rather than a hard failure — we back off and try again with exponential
    delay before giving up.
    """
    url = f'{API_BASE}/{path}'
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=_headers(), params=params,
                                    timeout=REQUEST_TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt == MAX_ATTEMPTS:
                raise
            wait = 3 * attempt
            print(f"  … {type(e).__name__} on {path}, retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        if response.status_code == 200:
            return response.json()
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
            wait = 3 * attempt
            print(f"  … HTTP {response.status_code} on {path}, retrying in {wait}s "
                  f"(attempt {attempt}/{MAX_ATTEMPTS})")
            time.sleep(wait)
            continue
        response.raise_for_status()
    return None


def _parse_date(value):
    """Upstream dates look like '1999/01/09'. Returns a date or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y/%m/%d').date()
    except (ValueError, TypeError):
        return None


def _extract_tcgplayer(payload):
    """
    Flatten a card's tcgplayer block into a single price dict.

    Upstream shape:
        {"url": ..., "prices": {"holofoil": {"low":..,"mid":..,"high":..,"market":..}}}
    """
    if not payload:
        return None
    prices = payload.get('prices') or {}
    variant = next((v for v in TCGPLAYER_VARIANT_PRIORITY if v in prices), None)
    if variant is None:
        # Unknown/new printing name -- take whatever is there rather than
        # dropping the price entirely.
        variant = next(iter(prices), None)
    if variant is None:
        return {'url': payload.get('url')}

    block = prices[variant] or {}
    return {
        'market': block.get('market'),
        'low': block.get('low'),
        'mid': block.get('mid'),
        'high': block.get('high'),
        'url': payload.get('url'),
    }


def _extract_cardmarket(payload):
    """Flatten a card's cardmarket block. Prices are EUR."""
    if not payload:
        return None
    prices = payload.get('prices') or {}
    return {
        'trend': prices.get('trendPrice'),
        'low': prices.get('lowPrice'),
        'url': payload.get('url'),
    }


def sync_sets():
    """
    Upsert every set. Cheap (one page) and safe to re-run.

    Returns (created, updated).
    """
    data = _get('sets', {'pageSize': PAGE_SIZE})
    if not data:
        return 0, 0

    created = updated = 0
    for item in data.get('data', []):
        images = item.get('images') or {}
        payload = {
            'name': item.get('name'),
            'series': item.get('series'),
            'printedTotal': item.get('printedTotal') or 0,
            'total': item.get('total') or 0,
            'releaseDate': _parse_date(item.get('releaseDate')),
            'logoUrl': images.get('logo'),
            'symbolUrl': images.get('symbol'),
            'ptcgoCode': item.get('ptcgoCode'),
        }

        existing = CardSet.get_by_id(item['id'])
        if existing:
            existing.update(payload)
            updated += 1
        else:
            CardSet(
                id=item['id'], name=payload['name'], series=payload['series'],
                printed_total=payload['printedTotal'], total=payload['total'],
                release_date=payload['releaseDate'], logo_url=payload['logoUrl'],
                symbol_url=payload['symbolUrl'], ptcgo_code=payload['ptcgoCode'],
            ).create()
            created += 1

    print(f"Sets: {created} created, {updated} updated")
    return created, updated


def sync_cards(set_id, record_snapshot=True):
    """
    Upsert every card in one set, page by page.

    record_snapshot writes a daily PriceSnapshot per card, which is what makes
    portfolio-value-over-time charts possible.

    Returns (created, updated).
    """
    created = updated = 0
    page = 1

    while True:
        data = _get('cards', {
            'q': f'set.id:{set_id}',
            'page': page,
            'pageSize': PAGE_SIZE,
        })
        if not data:
            break

        items = data.get('data', [])
        if not items:
            break

        for item in items:
            images = item.get('images') or {}
            tcgplayer = _extract_tcgplayer(item.get('tcgplayer'))
            cardmarket = _extract_cardmarket(item.get('cardmarket'))

            existing = Card.get_by_id(item['id'])
            if existing:
                existing.update({
                    'name': item.get('name'),
                    'number': item.get('number'),
                    'rarity': item.get('rarity'),
                    'supertype': item.get('supertype'),
                    'subtypes': item.get('subtypes'),
                    'types': item.get('types'),
                    'artist': item.get('artist'),
                    'flavorText': item.get('flavorText'),
                    'imageSmall': images.get('small'),
                    'imageLarge': images.get('large'),
                    'tcgplayer': tcgplayer,
                    'cardmarket': cardmarket,
                })
                card = existing
                updated += 1
            else:
                card = Card(
                    id=item['id'], set_id=set_id, name=item.get('name'),
                    number=item.get('number'), rarity=item.get('rarity'),
                    supertype=item.get('supertype'), subtypes=item.get('subtypes'),
                    types=item.get('types'), artist=item.get('artist'),
                    flavor_text=item.get('flavorText'),
                    image_small=images.get('small'), image_large=images.get('large'),
                )
                card.set_prices(tcgplayer, cardmarket)
                card.create()
                created += 1

            if record_snapshot and card:
                PriceSnapshot.record(
                    card.id,
                    tcgplayer_market=(tcgplayer or {}).get('market'),
                    cardmarket_trend=(cardmarket or {}).get('trend'),
                )

        db.session.commit()

        # Stop once we have consumed every page upstream reports.
        total_count = data.get('totalCount', 0)
        if page * PAGE_SIZE >= total_count:
            break
        page += 1
        time.sleep(0.3)  # be polite to a free API

    print(f"  {set_id}: {created} created, {updated} updated")
    return created, updated


def sync_all(limit_sets=None):
    """
    Full catalog sync: every set, then every card in every set.

    This is a long job -- roughly 20k cards across 150+ sets. limit_sets caps it
    for a quick smoke test.
    """
    sync_sets()

    sets = CardSet.get_all()
    if limit_sets:
        sets = sets[:limit_sets]

    total_created = total_updated = 0
    for index, card_set in enumerate(sets, start=1):
        print(f"[{index}/{len(sets)}] Syncing {card_set._name}…")
        try:
            created, updated = sync_cards(card_set.id)
            total_created += created
            total_updated += updated
        except requests.RequestException as e:
            # One bad set should not abort a multi-thousand-card sync.
            print(f"  ⚠️  {card_set.id} failed: {e}")

    print(f"✅ Catalog sync complete: {total_created} created, {total_updated} updated")
    return total_created, total_updated


def refresh_prices():
    """
    Re-pull pricing for sets we already track, without re-creating cards.

    Intended to run on a daily schedule so PriceSnapshot builds real history.
    """
    sets = CardSet.get_all()
    for index, card_set in enumerate(sets, start=1):
        print(f"[{index}/{len(sets)}] Refreshing prices for {card_set._name}…")
        try:
            sync_cards(card_set.id, record_snapshot=True)
        except requests.RequestException as e:
            print(f"  ⚠️  {card_set.id} failed: {e}")
    print("✅ Price refresh complete")


def register_cli(app_group_factory):
    """
    Register `flask tcg ...` commands.

    Takes the AppGroup factory from main.py so this module does not need to
    import Flask's CLI plumbing itself.
    """
    import click

    tcg_cli = app_group_factory('tcg')

    @tcg_cli.command('sync-sets')
    def _sync_sets():
        """Sync the set list from the Pokémon TCG API."""
        with app.app_context():
            sync_sets()

    @tcg_cli.command('sync-cards')
    @click.option('--set-id', required=True, help='Set id, e.g. base1')
    def _sync_cards(set_id):
        """Sync all cards in one set."""
        with app.app_context():
            sync_cards(set_id)

    @tcg_cli.command('sync-all')
    @click.option('--limit-sets', type=int, default=None,
                  help='Only sync the first N sets (smoke test)')
    def _sync_all(limit_sets):
        """Full catalog sync: every set and every card."""
        with app.app_context():
            sync_all(limit_sets=limit_sets)

    @tcg_cli.command('refresh-prices')
    def _refresh_prices():
        """Re-pull prices for all tracked sets and record daily snapshots."""
        with app.app_context():
            refresh_prices()

    return tcg_cli
