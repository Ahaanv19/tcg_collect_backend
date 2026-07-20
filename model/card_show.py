"""
TCG Collect — Card Show / Expo Directory

Tables: tcg_card_shows

The in-person side of the hobby. Collect-A-Con, regional card shows, and local
game-store events are scattered across Facebook groups, Instagram posts, and
promoter sites with no central listing. Neither TCGplayer nor any collection
tracker indexes them, which is exactly why this table exists: it is the bridge
between a digital want list and a physical booth.

Distance search uses the haversine formula in SQL-adjacent Python rather than
PostGIS, because the dataset is small (hundreds of shows, not millions) and
this keeps the app running on SQLite in development.
"""
from datetime import datetime, date
from math import radians, cos, sin, asin, sqrt

from sqlalchemy.exc import IntegrityError

from __init__ import app, db


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lng points, in miles."""
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlon, dlat = lon2 - lon1, lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 3956 * 2 * asin(sqrt(a))


class CardShow(db.Model):
    """
    A card show, expo, or convention.

    Fields:
        _name: e.g. "Collect-A-Con San Diego"
        _venue / _address / _city / _state / _zip_code: where it is
        _latitude / _longitude: for radius search
        _start_date / _end_date: multi-day shows are common
        _url: official page / ticket link
        _admission: door price in USD (0 = free)
        _vendor_count: approximate number of dealers, a proxy for how worthwhile
            the trip is
        _tags: JSON array, e.g. ["pokemon", "sports", "grading-onsite"]
    """
    __tablename__ = 'tcg_card_shows'

    id = db.Column(db.Integer, primary_key=True)
    _name = db.Column('name', db.String(250), nullable=False, index=True)
    _description = db.Column('description', db.Text, nullable=True)

    _venue = db.Column('venue', db.String(250), nullable=True)
    _address = db.Column('address', db.String(300), nullable=True)
    _city = db.Column('city', db.String(120), nullable=True, index=True)
    _state = db.Column('state', db.String(60), nullable=True, index=True)
    _zip_code = db.Column('zip_code', db.String(12), nullable=True)
    _country = db.Column('country', db.String(60), default='USA')
    _latitude = db.Column('latitude', db.Float, nullable=True)
    _longitude = db.Column('longitude', db.Float, nullable=True)

    _start_date = db.Column('start_date', db.Date, nullable=False, index=True)
    _end_date = db.Column('end_date', db.Date, nullable=True)

    _url = db.Column('url', db.String(500), nullable=True)
    _image_url = db.Column('image_url', db.String(500), nullable=True)
    _admission = db.Column('admission', db.Float, nullable=True)
    _vendor_count = db.Column('vendor_count', db.Integer, nullable=True)
    _tags = db.Column('tags', db.JSON, nullable=True)

    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)

    def __init__(self, name, start_date, end_date=None, description=None, venue=None,
                 address=None, city=None, state=None, zip_code=None, country='USA',
                 latitude=None, longitude=None, url=None, image_url=None,
                 admission=None, vendor_count=None, tags=None):
        self._name = name
        self._start_date = start_date
        self._end_date = end_date
        self._description = description
        self._venue = venue
        self._address = address
        self._city = city
        self._state = state
        self._zip_code = zip_code
        self._country = country
        self._latitude = latitude
        self._longitude = longitude
        self._url = url
        self._image_url = image_url
        self._admission = admission
        self._vendor_count = vendor_count
        self._tags = tags

    @property
    def is_upcoming(self):
        end = self._end_date or self._start_date
        return end >= date.today()

    @property
    def days_until(self):
        """Days until the show opens. Negative once it has started/passed."""
        return (self._start_date - date.today()).days

    # --- CRUD ---

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def read(self, distance_miles=None):
        data = {
            'id': self.id,
            'name': self._name,
            'description': self._description,
            'venue': self._venue,
            'address': self._address,
            'city': self._city,
            'state': self._state,
            'zipCode': self._zip_code,
            'country': self._country,
            'latitude': self._latitude,
            'longitude': self._longitude,
            'startDate': self._start_date.isoformat() if self._start_date else None,
            'endDate': self._end_date.isoformat() if self._end_date else None,
            'url': self._url,
            'imageUrl': self._image_url,
            'admission': self._admission,
            'vendorCount': self._vendor_count,
            'tags': self._tags or [],
            'isUpcoming': self.is_upcoming,
            'daysUntil': self.days_until,
        }
        if distance_miles is not None:
            data['distanceMiles'] = round(distance_miles, 1)
        return data

    def update(self, data):
        for key, attr in (('name', '_name'), ('description', '_description'),
                          ('venue', '_venue'), ('address', '_address'),
                          ('city', '_city'), ('state', '_state'),
                          ('zipCode', '_zip_code'), ('country', '_country'),
                          ('latitude', '_latitude'), ('longitude', '_longitude'),
                          ('url', '_url'), ('imageUrl', '_image_url'),
                          ('admission', '_admission'), ('vendorCount', '_vendor_count'),
                          ('tags', '_tags')):
            if key in data:
                setattr(self, attr, data[key])
        if data.get('startDate'):
            self._start_date = data['startDate']
        if data.get('endDate'):
            self._end_date = data['endDate']
        try:
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def delete(self):
        try:
            db.session.delete(self)
            db.session.commit()
            return True
        except Exception:
            db.session.rollback()
            return False

    # --- Query helpers ---

    @staticmethod
    def get_by_id(show_id):
        return CardShow.query.get(show_id)

    @staticmethod
    def search(query=None, state=None, city=None, upcoming_only=True,
               start_after=None, start_before=None,
               latitude=None, longitude=None, radius_miles=None, limit=100):
        """
        Find shows by text, location, and date window.

        When latitude/longitude/radius_miles are supplied the results are
        filtered and sorted by real distance; otherwise they come back in date
        order (soonest first), which is what a browse page wants.
        """
        q = CardShow.query

        if query:
            like = f'%{query}%'
            q = q.filter(db.or_(CardShow._name.ilike(like),
                                CardShow._venue.ilike(like),
                                CardShow._description.ilike(like)))
        if state:
            q = q.filter(CardShow._state.ilike(state))
        if city:
            q = q.filter(CardShow._city.ilike(city))
        if upcoming_only:
            q = q.filter(db.func.coalesce(CardShow._end_date, CardShow._start_date) >= date.today())
        if start_after:
            q = q.filter(CardShow._start_date >= start_after)
        if start_before:
            q = q.filter(CardShow._start_date <= start_before)

        shows = q.order_by(CardShow._start_date.asc()).limit(limit).all()

        # Radius filter runs in Python: the candidate set is already narrowed by
        # date/state above, so this stays cheap and works identically on SQLite
        # and MySQL.
        if latitude is not None and longitude is not None and radius_miles:
            within = []
            for show in shows:
                if show._latitude is None or show._longitude is None:
                    continue
                distance = haversine_miles(latitude, longitude, show._latitude, show._longitude)
                if distance <= radius_miles:
                    within.append((show, distance))
            within.sort(key=lambda pair: pair[1])
            return [(s, d) for s, d in within]

        return [(s, None) for s in shows]

    @staticmethod
    def upcoming(limit=10):
        return (CardShow.query
                .filter(db.func.coalesce(CardShow._end_date, CardShow._start_date) >= date.today())
                .order_by(CardShow._start_date.asc())
                .limit(limit)
                .all())

    @staticmethod
    def all_states():
        rows = db.session.query(CardShow._state).distinct().all()
        return sorted({r[0] for r in rows if r[0]})


def initCardShows():
    """
    Create the table and seed a starter directory of real, recurring shows.

    Dates here are placeholders for the 2026 circuit; the admin endpoints let a
    user correct them without a redeploy.
    """
    with app.app_context():
        db.create_all()

        if CardShow.query.first():
            print("✅ Card shows already populated, skipping seed")
            return

        seeds = [
            CardShow(
                name="Collect-A-Con San Diego",
                description="One of the largest traveling TCG and collectibles conventions. "
                            "Hundreds of vendors, on-site grading submission, and artist alley.",
                venue="San Diego Convention Center", address="111 W Harbor Dr",
                city="San Diego", state="CA", zip_code="92101",
                latitude=32.7057, longitude=-117.1611,
                start_date=date(2026, 9, 12), end_date=date(2026, 9, 13),
                url="https://collectacon.com", admission=35.0, vendor_count=400,
                tags=["pokemon", "anime", "grading-onsite", "artist-alley"],
            ),
            CardShow(
                name="Collect-A-Con Los Angeles",
                description="LA stop of the Collect-A-Con circuit. Heavy Pokémon vendor presence.",
                venue="Los Angeles Convention Center", address="1201 S Figueroa St",
                city="Los Angeles", state="CA", zip_code="90015",
                latitude=34.0403, longitude=-118.2696,
                start_date=date(2026, 11, 7), end_date=date(2026, 11, 8),
                url="https://collectacon.com", admission=40.0, vendor_count=500,
                tags=["pokemon", "sports", "grading-onsite"],
            ),
            CardShow(
                name="Pokémon TCG Regional Championships — Portland",
                description="Official Play! Pokémon regional. Large vendor hall alongside the tournament.",
                venue="Oregon Convention Center", address="777 NE Martin Luther King Jr Blvd",
                city="Portland", state="OR", zip_code="97232",
                latitude=45.5285, longitude=-122.6633,
                start_date=date(2026, 8, 22), end_date=date(2026, 8, 23),
                url="https://www.pokemon.com/us/play-pokemon", admission=0.0, vendor_count=60,
                tags=["pokemon", "tournament", "official"],
            ),
            CardShow(
                name="San Diego Sports & Trading Card Show",
                description="Monthly local show. Smaller and cheaper than the big conventions — "
                            "good for raw singles and bulk.",
                venue="Scottish Rite Event Center", address="1895 Camino Del Rio S",
                city="San Diego", state="CA", zip_code="92108",
                latitude=32.7645, longitude=-117.1466,
                start_date=date(2026, 8, 2), end_date=date(2026, 8, 2),
                url="https://example.com/sd-card-show", admission=5.0, vendor_count=45,
                tags=["pokemon", "sports", "local", "monthly"],
            ),
            CardShow(
                name="Anaheim Collectibles Expo",
                description="Regional expo with a strong vintage Pokémon and graded-slab presence.",
                venue="Anaheim Convention Center", address="800 W Katella Ave",
                city="Anaheim", state="CA", zip_code="92802",
                latitude=33.8003, longitude=-117.9192,
                start_date=date(2026, 10, 17), end_date=date(2026, 10, 18),
                url="https://example.com/anaheim-expo", admission=25.0, vendor_count=220,
                tags=["pokemon", "vintage", "graded"],
            ),
            CardShow(
                name="Collect-A-Con Dallas",
                description="Texas flagship stop. Consistently the biggest vendor floor of the tour.",
                venue="Kay Bailey Hutchison Convention Center", address="650 S Griffin St",
                city="Dallas", state="TX", zip_code="75202",
                latitude=32.7745, longitude=-96.8006,
                start_date=date(2026, 12, 5), end_date=date(2026, 12, 6),
                url="https://collectacon.com", admission=40.0, vendor_count=550,
                tags=["pokemon", "anime", "sports", "grading-onsite"],
            ),
        ]

        for show in seeds:
            if show.create():
                print(f"  Seeded show: {show._name} ({show._city}, {show._state})")

        print("✅ Card show directory seeded")
