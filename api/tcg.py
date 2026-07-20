"""
TCG Collect — Catalog API

Public, read-only endpoints for browsing the card catalog. No auth required:
price lookup is the top of the funnel and should work before signup.

Routes (all under /api):
    GET  /sets                     list sets, optional ?series=
    GET  /sets/<set_id>            one set
    GET  /sets/<set_id>/cards      cards in a set
    GET  /cards                    paginated search
    GET  /cards/<card_id>          one card + buy options + price history
    GET  /cards/<card_id>/prices   price history only
    GET  /movers                   biggest price movements
    GET  /catalog/filters          rarities/series/types for filter menus
"""
from flask import Blueprint, request, jsonify
from flask_restful import Api, Resource

from __init__ import app, db
from model.tcg import CardSet, Card, PriceSnapshot

tcg_api = Blueprint('tcg_api', __name__, url_prefix='/api')
api = Api(tcg_api, errors={})


def _int_arg(name, default):
    """Read an int query param, falling back on anything unparseable."""
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_arg(name, default=None):
    try:
        value = request.args.get(name)
        return float(value) if value not in (None, '') else default
    except (TypeError, ValueError):
        return default


class SetAPI:
    class _LIST(Resource):
        def get(self):
            series = request.args.get('series')
            # ?nonEmpty=true hides sets that have no cards synced yet, so the
            # UI never shows a set that leads to an empty grid.
            non_empty = request.args.get('nonEmpty', 'false').lower() == 'true'

            sets = CardSet.get_all(series=series)
            counts = Card.counts_by_set()
            payload = []
            for s in sets:
                count = counts.get(s.id, 0)
                if non_empty and count == 0:
                    continue
                payload.append({**s.read(), 'cardCount': count})
            return jsonify(payload)

    class _ONE(Resource):
        def get(self, set_id):
            card_set = CardSet.get_by_id(set_id)
            if not card_set:
                return {'message': 'Set not found'}, 404
            data = card_set.read()
            data['cardCount'] = card_set.cards.count()
            return jsonify(data)

    class _CARDS(Resource):
        def get(self, set_id):
            card_set = CardSet.get_by_id(set_id)
            if not card_set:
                return {'message': 'Set not found'}, 404

            page = _int_arg('page', 1)
            per_page = min(_int_arg('perPage', 60), 250)
            results = Card.search(set_id=set_id, sort=request.args.get('sort', 'number'),
                                  page=page, per_page=per_page)
            return jsonify({
                'set': card_set.read(),
                'cards': [c.read() for c in results.items],
                'page': results.page,
                'pages': results.pages,
                'total': results.total,
            })


class CardAPI:
    class _SEARCH(Resource):
        def get(self):
            page = _int_arg('page', 1)
            per_page = min(_int_arg('perPage', 48), 250)

            results = Card.search(
                name=request.args.get('name') or request.args.get('q'),
                set_id=request.args.get('setId'),
                rarity=request.args.get('rarity'),
                card_type=request.args.get('type'),
                min_price=_float_arg('minPrice'),
                max_price=_float_arg('maxPrice'),
                sort=request.args.get('sort', 'name'),
                page=page,
                per_page=per_page,
            )
            return jsonify({
                'cards': [c.read() for c in results.items],
                'page': results.page,
                'pages': results.pages,
                'total': results.total,
                'perPage': per_page,
            })

    class _ONE(Resource):
        def get(self, card_id):
            card = Card.get_by_id(card_id)
            if not card:
                return {'message': 'Card not found'}, 404

            data = card.read(include_buy_options=True)
            data['priceHistory'] = [s.read() for s in PriceSnapshot.history(card_id, limit=90)]
            return jsonify(data)

    class _PRICES(Resource):
        def get(self, card_id):
            card = Card.get_by_id(card_id)
            if not card:
                return {'message': 'Card not found'}, 404

            days = min(_int_arg('days', 90), 365)
            history = [s.read() for s in PriceSnapshot.history(card_id, limit=days)]
            return jsonify({
                'cardId': card_id,
                'current': card.market_price,
                'buyOptions': card.buy_options(),
                'history': history,
            })


class MoversAPI(Resource):
    """
    Biggest price swings over a window, computed from stored snapshots.

    This is the kind of view a marketplace has no incentive to build -- it tells
    you when NOT to buy -- so it is a natural differentiator.
    """
    def get(self):
        days = min(_int_arg('days', 7), 90)
        limit = min(_int_arg('limit', 20), 100)
        direction = request.args.get('direction', 'both')  # up | down | both

        # Compare each card's newest snapshot against its oldest inside the window.
        movers = []
        cards = (Card.query
                 .filter(Card._tcgplayer_market.isnot(None))
                 .limit(2000)
                 .all())

        for card in cards:
            history = PriceSnapshot.history(card.id, limit=days)
            if len(history) < 2:
                continue
            start = history[0]._tcgplayer_market
            end = history[-1]._tcgplayer_market
            if not start or not end:
                continue

            change = end - start
            percent = (change / start) * 100
            movers.append({
                'card': card.read(),
                'startPrice': round(start, 2),
                'endPrice': round(end, 2),
                'change': round(change, 2),
                'changePercent': round(percent, 1),
            })

        if direction == 'up':
            movers = [m for m in movers if m['change'] > 0]
        elif direction == 'down':
            movers = [m for m in movers if m['change'] < 0]

        movers.sort(key=lambda m: abs(m['changePercent']), reverse=True)
        return jsonify({'days': days, 'movers': movers[:limit]})


class FiltersAPI(Resource):
    """
    Everything the browse page needs to populate its filter dropdowns.

    Only sets that actually have cards are listed — picking an empty set from a
    dropdown would just show an empty grid.
    """
    def get(self):
        counts = Card.counts_by_set()
        sets = [{'id': s.id, 'name': s._name, 'series': s._series,
                 'cardCount': counts.get(s.id, 0)}
                for s in CardSet.get_all()
                if counts.get(s.id, 0) > 0]
        return jsonify({
            'rarities': Card.all_rarities(),
            'series': sorted({s['series'] for s in sets if s['series']}),
            'sets': sets,
            'types': ['Colorless', 'Darkness', 'Dragon', 'Fairy', 'Fighting',
                      'Fire', 'Grass', 'Lightning', 'Metal', 'Psychic', 'Water'],
        })


# Explicit endpoint names: flask_restful derives the endpoint from the class
# name, so two nested classes both called _ONE would collide inside this
# blueprint.
api.add_resource(SetAPI._LIST, '/sets', endpoint='set_list')
api.add_resource(SetAPI._ONE, '/sets/<string:set_id>', endpoint='set_detail')
api.add_resource(SetAPI._CARDS, '/sets/<string:set_id>/cards', endpoint='set_cards')
api.add_resource(CardAPI._SEARCH, '/cards', endpoint='card_search')
api.add_resource(CardAPI._ONE, '/cards/<string:card_id>', endpoint='card_detail')
api.add_resource(CardAPI._PRICES, '/cards/<string:card_id>/prices', endpoint='card_prices')
api.add_resource(MoversAPI, '/movers', endpoint='movers')
api.add_resource(FiltersAPI, '/catalog/filters', endpoint='catalog_filters')
