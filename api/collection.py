"""
TCG Collect — Collection, Want List, and Show Mode API

All endpoints require auth: this is the user's own portfolio.

Routes (all under /api):
    GET/POST      /collection                  list / add owned cards
    PUT/DELETE    /collection/<item_id>        edit / remove a lot
    GET           /collection/summary          portfolio headline numbers
    GET           /collection/completion/<set> set completion + missing cards

    GET/POST      /wantlist                    list / add wanted cards
    PUT/DELETE    /wantlist/<item_id>          edit / remove
    GET           /wantlist/budget?budget=200  greedy show-budget planner

    GET           /show-mode                   want list + live prices, for a booth
"""
from datetime import datetime

from flask import Blueprint, request, jsonify, g
from flask_restful import Api, Resource

from __init__ import app, db
from api.jwt_authorize import token_required
from model.tcg import Card
from model.collection import CollectionItem, WantListItem, CONDITIONS

collection_api = Blueprint('collection_api', __name__, url_prefix='/api')
api = Api(collection_api, errors={})


def _parse_date(value):
    """Accept an ISO date string; return a date or None."""
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


class CollectionAPI:
    class _CRUD(Resource):
        @token_required()
        def get(self):
            """List the current user's collection, optionally filtered by set."""
            items = CollectionItem.for_user(g.current_user.id,
                                            set_id=request.args.get('setId'))
            return jsonify([i.read() for i in items])

        @token_required()
        def post(self):
            """Add a card to the collection."""
            body = request.get_json() or {}

            card_id = body.get('cardId')
            if not card_id:
                return {'message': 'cardId is required'}, 400
            if not Card.get_by_id(card_id):
                return {'message': f'Unknown card: {card_id}'}, 404

            condition = body.get('condition', 'Near Mint')
            if condition not in CONDITIONS:
                return {'message': f'condition must be one of {CONDITIONS}'}, 400

            quantity = body.get('quantity', 1)
            if not isinstance(quantity, int) or quantity < 1:
                return {'message': 'quantity must be a positive integer'}, 400

            item = CollectionItem(
                user_id=g.current_user.id,
                card_id=card_id,
                quantity=quantity,
                condition=condition,
                is_foil=bool(body.get('isFoil', False)),
                purchase_price=body.get('purchasePrice'),
                acquired_date=_parse_date(body.get('acquiredDate')),
                grader=body.get('grader'),
                grade=body.get('grade'),
                notes=body.get('notes'),
            )
            if not item.create():
                return {'message': 'Could not add card to collection'}, 500
            return jsonify(item.read())

    class _ITEM(Resource):
        @token_required()
        def put(self, item_id):
            item = CollectionItem.get_by_id(item_id)
            if not item:
                return {'message': 'Item not found'}, 404
            if item._user_id != g.current_user.id:
                return {'message': 'Not your collection item'}, 403

            body = request.get_json() or {}
            if 'condition' in body and body['condition'] not in CONDITIONS:
                return {'message': f'condition must be one of {CONDITIONS}'}, 400
            if 'acquiredDate' in body:
                body['acquiredDate'] = _parse_date(body['acquiredDate'])

            if not item.update(body):
                return {'message': 'Update failed'}, 500
            return jsonify(item.read())

        @token_required()
        def delete(self, item_id):
            item = CollectionItem.get_by_id(item_id)
            if not item:
                return {'message': 'Item not found'}, 404
            if item._user_id != g.current_user.id:
                return {'message': 'Not your collection item'}, 403
            if not item.delete():
                return {'message': 'Delete failed'}, 500
            return {'message': 'Removed from collection'}, 200

    class _SUMMARY(Resource):
        @token_required()
        def get(self):
            """Portfolio headline: value, cost basis, unrealized gain/loss."""
            return jsonify(CollectionItem.portfolio_summary(g.current_user.id))

    class _COMPLETION(Resource):
        @token_required()
        def get(self, set_id):
            """How far through a set the user is, and what's missing."""
            result = CollectionItem.set_completion(g.current_user.id, set_id)
            if result is None:
                return {'message': 'Set not found'}, 404
            return jsonify(result)


class WantListAPI:
    class _CRUD(Resource):
        @token_required()
        def get(self):
            items = WantListItem.for_user(g.current_user.id)
            return jsonify([i.read() for i in items])

        @token_required()
        def post(self):
            body = request.get_json() or {}

            card_id = body.get('cardId')
            if not card_id:
                return {'message': 'cardId is required'}, 400
            if not Card.get_by_id(card_id):
                return {'message': f'Unknown card: {card_id}'}, 404

            # Unique constraint on (user, card) -- surface the conflict rather
            # than letting create() fail silently.
            existing = (WantListItem.query
                        .filter_by(_user_id=g.current_user.id, _card_id=card_id)
                        .first())
            if existing:
                return {'message': 'Card is already on your want list',
                        'item': existing.read()}, 409

            item = WantListItem(
                user_id=g.current_user.id,
                card_id=card_id,
                max_price=body.get('maxPrice'),
                priority=body.get('priority', 3),
                notes=body.get('notes'),
            )
            if not item.create():
                return {'message': 'Could not add to want list'}, 500
            return jsonify(item.read())

    class _ITEM(Resource):
        @token_required()
        def put(self, item_id):
            item = WantListItem.get_by_id(item_id)
            if not item:
                return {'message': 'Item not found'}, 404
            if item._user_id != g.current_user.id:
                return {'message': 'Not your want list item'}, 403
            if not item.update(request.get_json() or {}):
                return {'message': 'Update failed'}, 500
            return jsonify(item.read())

        @token_required()
        def delete(self, item_id):
            item = WantListItem.get_by_id(item_id)
            if not item:
                return {'message': 'Item not found'}, 404
            if item._user_id != g.current_user.id:
                return {'message': 'Not your want list item'}, 403
            if not item.delete():
                return {'message': 'Delete failed'}, 500
            return {'message': 'Removed from want list'}, 200

    class _BUDGET(Resource):
        @token_required()
        def get(self):
            """
            Given a budget, plan which want-list cards to buy at a show.
            """
            try:
                budget = float(request.args.get('budget', 0))
            except (TypeError, ValueError):
                return {'message': 'budget must be a number'}, 400
            if budget <= 0:
                return {'message': 'budget must be greater than 0'}, 400

            return jsonify(WantListItem.budget_plan(g.current_user.id, budget))


class ShowModeAPI(Resource):
    """
    The feature this whole app exists for.

    You are standing at a booth at Collect-A-Con. A vendor quotes you a price.
    This endpoint gives you, in one request: everything on your want list, the
    current market price of each, your own max bid, and whether the market has
    already fallen below your ceiling. Sorted by priority so the cards you care
    about most are at the top of the phone screen.
    """
    @token_required()
    def get(self):
        items = WantListItem.for_user(g.current_user.id)

        payload = []
        for item in items:
            row = item.read()
            card = item.card
            # Cheapest listed option across vendors, for haggling leverage.
            options = card.buy_options() if card else []
            priced = [o for o in options if o.get('price') is not None]
            row['cheapestVendor'] = priced[0] if priced else None
            payload.append(row)

        deals = [r for r in payload if r.get('isGoodDeal')]

        return jsonify({
            'wantList': payload,
            'count': len(payload),
            'atOrBelowMaxPrice': len(deals),
            'generatedAt': datetime.utcnow().isoformat(),
        })


# Explicit endpoint names: flask_restful derives the endpoint from the class
# name, so CollectionAPI._CRUD and WantListAPI._CRUD would otherwise collide.
api.add_resource(CollectionAPI._CRUD, '/collection', endpoint='collection_list')
api.add_resource(CollectionAPI._ITEM, '/collection/<int:item_id>', endpoint='collection_item')
api.add_resource(CollectionAPI._SUMMARY, '/collection/summary', endpoint='collection_summary')
api.add_resource(CollectionAPI._COMPLETION, '/collection/completion/<string:set_id>',
                 endpoint='collection_completion')

api.add_resource(WantListAPI._CRUD, '/wantlist', endpoint='wantlist_list')
api.add_resource(WantListAPI._ITEM, '/wantlist/<int:item_id>', endpoint='wantlist_item')
api.add_resource(WantListAPI._BUDGET, '/wantlist/budget', endpoint='wantlist_budget')

api.add_resource(ShowModeAPI, '/show-mode', endpoint='show_mode')
