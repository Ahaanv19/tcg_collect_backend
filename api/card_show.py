"""
TCG Collect — Card Show Directory API

Public read, admin write. Finding shows should not require an account; adding or
editing one should, so the directory does not fill with spam.

Routes (all under /api):
    GET    /shows                  search: ?q=&state=&city=&lat=&lng=&radius=&upcoming=
    GET    /shows/<show_id>        one show
    POST   /shows                  create (Admin)
    PUT    /shows/<show_id>        edit (Admin)
    DELETE /shows/<show_id>        remove (Admin)
    GET    /shows/filters          states + upcoming count, for filter menus
"""
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_restful import Api, Resource

from __init__ import app, db
from api.jwt_authorize import token_required
from model.card_show import CardShow

card_show_api = Blueprint('card_show_api', __name__, url_prefix='/api')
api = Api(card_show_api, errors={})


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _float_arg(name):
    try:
        value = request.args.get(name)
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


class ShowAPI:
    class _SEARCH(Resource):
        def get(self):
            """
            Search shows by text, state/city, date window, or radius.

            Supplying lat + lng + radius switches results to distance-sorted and
            attaches distanceMiles to each row.
            """
            upcoming = request.args.get('upcoming', 'true').lower() != 'false'
            try:
                limit = min(int(request.args.get('limit', 100)), 250)
            except (TypeError, ValueError):
                limit = 100

            results = CardShow.search(
                query=request.args.get('q'),
                state=request.args.get('state'),
                city=request.args.get('city'),
                upcoming_only=upcoming,
                start_after=_parse_date(request.args.get('startAfter')),
                start_before=_parse_date(request.args.get('startBefore')),
                latitude=_float_arg('lat'),
                longitude=_float_arg('lng'),
                radius_miles=_float_arg('radius'),
                limit=limit,
            )
            return jsonify({
                'shows': [show.read(distance) for show, distance in results],
                'count': len(results),
            })

        @token_required(roles=['Admin'])
        def post(self):
            body = request.get_json() or {}

            name = body.get('name')
            start_date = _parse_date(body.get('startDate'))
            if not name or not start_date:
                return {'message': 'name and startDate (YYYY-MM-DD) are required'}, 400

            show = CardShow(
                name=name,
                start_date=start_date,
                end_date=_parse_date(body.get('endDate')),
                description=body.get('description'),
                venue=body.get('venue'),
                address=body.get('address'),
                city=body.get('city'),
                state=body.get('state'),
                zip_code=body.get('zipCode'),
                country=body.get('country', 'USA'),
                latitude=body.get('latitude'),
                longitude=body.get('longitude'),
                url=body.get('url'),
                image_url=body.get('imageUrl'),
                admission=body.get('admission'),
                vendor_count=body.get('vendorCount'),
                tags=body.get('tags'),
            )
            if not show.create():
                return {'message': 'Could not create show'}, 500
            return jsonify(show.read())

    class _ONE(Resource):
        def get(self, show_id):
            show = CardShow.get_by_id(show_id)
            if not show:
                return {'message': 'Show not found'}, 404
            return jsonify(show.read())

        @token_required(roles=['Admin'])
        def put(self, show_id):
            show = CardShow.get_by_id(show_id)
            if not show:
                return {'message': 'Show not found'}, 404

            body = request.get_json() or {}
            if 'startDate' in body:
                body['startDate'] = _parse_date(body['startDate'])
            if 'endDate' in body:
                body['endDate'] = _parse_date(body['endDate'])

            if not show.update(body):
                return {'message': 'Update failed'}, 500
            return jsonify(show.read())

        @token_required(roles=['Admin'])
        def delete(self, show_id):
            show = CardShow.get_by_id(show_id)
            if not show:
                return {'message': 'Show not found'}, 404
            if not show.delete():
                return {'message': 'Delete failed'}, 500
            return {'message': 'Show removed'}, 200

    class _FILTERS(Resource):
        def get(self):
            upcoming = CardShow.upcoming(limit=250)
            return jsonify({
                'states': CardShow.all_states(),
                'upcomingCount': len(upcoming),
                'next': upcoming[0].read() if upcoming else None,
            })


api.add_resource(ShowAPI._SEARCH, '/shows', endpoint='show_search')
api.add_resource(ShowAPI._ONE, '/shows/<int:show_id>', endpoint='show_detail')
api.add_resource(ShowAPI._FILTERS, '/shows/filters', endpoint='show_filters')
