"""
TCG Collect — Card Catalog Models

Tables: tcg_sets, tcg_cards, tcg_price_snapshots

Mirrors the Pokemon TCG API (pokemontcg.io) catalog into local storage so that
collection tracking, set-completion math, and price history all run against our
own database instead of hammering a rate-limited upstream API.

Pricing philosophy (the differentiator):
  TCGplayer shows you TCGplayer's price. We store every vendor's price side by
  side -- TCGplayer (USD) and Cardmarket (EUR) come free with the upstream API --
  so a collector can see who is actually cheapest before they buy, online or at
  a card show booth.
"""
from datetime import datetime, date

from sqlalchemy.exc import IntegrityError

from __init__ import app, db


class CardSet(db.Model):
    """
    A Pokemon TCG set (e.g. "Base", "Scarlet & Violet 151").

    The primary key is the upstream API's string id ("base1", "sv3pt5") so that
    syncs are idempotent -- re-running a sync updates rows rather than duplicating
    them.

    Fields:
        id: upstream set id, e.g. "base1"
        _name: display name, e.g. "Base"
        _series: parent series, e.g. "Base" / "Scarlet & Violet"
        _printed_total: card count as printed on the cards ("102" in "4/102")
        _total: true card count including secret rares
        _release_date: set release date
        _logo_url / _symbol_url: set art from upstream
        _ptcgo_code: short code, e.g. "BS", "MEW"
    """
    __tablename__ = 'tcg_sets'

    id = db.Column(db.String(50), primary_key=True)
    _name = db.Column('name', db.String(200), nullable=False)
    _series = db.Column('series', db.String(100), nullable=True)
    _printed_total = db.Column('printed_total', db.Integer, default=0)
    _total = db.Column('total', db.Integer, default=0)
    _release_date = db.Column('release_date', db.Date, nullable=True)
    _logo_url = db.Column('logo_url', db.String(500), nullable=True)
    _symbol_url = db.Column('symbol_url', db.String(500), nullable=True)
    _ptcgo_code = db.Column('ptcgo_code', db.String(20), nullable=True)
    _synced_at = db.Column('synced_at', db.DateTime, default=datetime.utcnow)

    cards = db.relationship('Card', backref='card_set', lazy='dynamic',
                            cascade='all, delete-orphan')

    def __init__(self, id, name, series=None, printed_total=0, total=0,
                 release_date=None, logo_url=None, symbol_url=None, ptcgo_code=None):
        self.id = id
        self._name = name
        self._series = series
        self._printed_total = printed_total
        self._total = total
        self._release_date = release_date
        self._logo_url = logo_url
        self._symbol_url = symbol_url
        self._ptcgo_code = ptcgo_code

    # --- CRUD ---

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def read(self):
        return {
            'id': self.id,
            'name': self._name,
            'series': self._series,
            'printedTotal': self._printed_total,
            'total': self._total,
            'releaseDate': self._release_date.isoformat() if self._release_date else None,
            'logoUrl': self._logo_url,
            'symbolUrl': self._symbol_url,
            'ptcgoCode': self._ptcgo_code,
        }

    def update(self, data):
        """Update from an upstream sync payload. Keys are camelCase."""
        for key, attr in (('name', '_name'), ('series', '_series'),
                          ('printedTotal', '_printed_total'), ('total', '_total'),
                          ('logoUrl', '_logo_url'), ('symbolUrl', '_symbol_url'),
                          ('ptcgoCode', '_ptcgo_code')):
            if key in data and data[key] is not None:
                setattr(self, attr, data[key])
        if data.get('releaseDate'):
            self._release_date = data['releaseDate']
        self._synced_at = datetime.utcnow()
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
    def get_by_id(set_id):
        return CardSet.query.get(set_id)

    @staticmethod
    def get_all(series=None):
        query = CardSet.query
        if series:
            query = query.filter_by(_series=series)
        return query.order_by(CardSet._release_date.desc().nullslast()).all()

    @staticmethod
    def all_series():
        """Distinct series names, newest first, for filter menus."""
        rows = db.session.query(CardSet._series).distinct().all()
        return sorted({r[0] for r in rows if r[0]})


class Card(db.Model):
    """
    A single Pokemon card, with cross-vendor pricing denormalized onto the row.

    We keep the latest price from each vendor directly on the card so the browse
    grid renders in one query. Historical movement lives in PriceSnapshot.

    Fields:
        id: upstream card id, e.g. "base1-4" (Charizard)
        _set_id: FK to tcg_sets.id
        _number: printed collector number ("4" in "4/102")
        _rarity: e.g. "Rare Holo", "Illustration Rare"
        _types / _subtypes: JSON arrays from upstream
        _image_small / _image_large: card art
        _tcgplayer_* : USD market/low/mid/high + buy URL
        _cardmarket_*: EUR trend/low + buy URL
    """
    __tablename__ = 'tcg_cards'

    id = db.Column(db.String(50), primary_key=True)
    _set_id = db.Column('set_id', db.String(50), db.ForeignKey('tcg_sets.id'), nullable=False)
    _name = db.Column('name', db.String(200), nullable=False, index=True)
    _number = db.Column('number', db.String(20), nullable=True)
    _rarity = db.Column('rarity', db.String(100), nullable=True, index=True)
    _supertype = db.Column('supertype', db.String(50), nullable=True)
    _subtypes = db.Column('subtypes', db.JSON, nullable=True)
    _types = db.Column('types', db.JSON, nullable=True)
    _artist = db.Column('artist', db.String(150), nullable=True)
    _flavor_text = db.Column('flavor_text', db.Text, nullable=True)

    _image_small = db.Column('image_small', db.String(500), nullable=True)
    _image_large = db.Column('image_large', db.String(500), nullable=True)

    # Cross-vendor pricing -- the reason this app beats a single marketplace.
    _tcgplayer_market = db.Column('tcgplayer_market', db.Float, nullable=True)
    _tcgplayer_low = db.Column('tcgplayer_low', db.Float, nullable=True)
    _tcgplayer_mid = db.Column('tcgplayer_mid', db.Float, nullable=True)
    _tcgplayer_high = db.Column('tcgplayer_high', db.Float, nullable=True)
    _tcgplayer_url = db.Column('tcgplayer_url', db.String(500), nullable=True)

    _cardmarket_trend = db.Column('cardmarket_trend', db.Float, nullable=True)
    _cardmarket_low = db.Column('cardmarket_low', db.Float, nullable=True)
    _cardmarket_url = db.Column('cardmarket_url', db.String(500), nullable=True)

    _price_updated_at = db.Column('price_updated_at', db.DateTime, nullable=True)
    _synced_at = db.Column('synced_at', db.DateTime, default=datetime.utcnow)

    snapshots = db.relationship('PriceSnapshot', backref='card', lazy='dynamic',
                                cascade='all, delete-orphan')

    def __init__(self, id, set_id, name, number=None, rarity=None, supertype=None,
                 subtypes=None, types=None, artist=None, flavor_text=None,
                 image_small=None, image_large=None):
        self.id = id
        self._set_id = set_id
        self._name = name
        self._number = number
        self._rarity = rarity
        self._supertype = supertype
        self._subtypes = subtypes
        self._types = types
        self._artist = artist
        self._flavor_text = flavor_text
        self._image_small = image_small
        self._image_large = image_large

    # --- Pricing ---

    def set_prices(self, tcgplayer=None, cardmarket=None):
        """
        Apply a pricing payload from the upstream sync.

        tcgplayer: {"market":..,"low":..,"mid":..,"high":..,"url":..}
        cardmarket: {"trend":..,"low":..,"url":..}
        """
        if tcgplayer:
            self._tcgplayer_market = tcgplayer.get('market')
            self._tcgplayer_low = tcgplayer.get('low')
            self._tcgplayer_mid = tcgplayer.get('mid')
            self._tcgplayer_high = tcgplayer.get('high')
            self._tcgplayer_url = tcgplayer.get('url')
        if cardmarket:
            self._cardmarket_trend = cardmarket.get('trend')
            self._cardmarket_low = cardmarket.get('low')
            self._cardmarket_url = cardmarket.get('url')
        self._price_updated_at = datetime.utcnow()

    @property
    def market_price(self):
        """
        Best single number to show in a grid. Prefers TCGplayer market (USD),
        falls back to Cardmarket trend. Returns None if the card has never priced.
        """
        return self._tcgplayer_market or self._cardmarket_trend

    def buy_options(self):
        """
        Every place you can buy this card, cheapest first.

        This is the "where to buy it" feature. TCGplayer will only ever show you
        TCGplayer; we rank vendors against each other and always include an eBay
        sold-listings search so the user can sanity-check against real completions.
        """
        options = []
        if self._tcgplayer_url:
            options.append({
                'vendor': 'TCGplayer',
                'currency': 'USD',
                'price': self._tcgplayer_market,
                'lowPrice': self._tcgplayer_low,
                'url': self._tcgplayer_url,
            })
        if self._cardmarket_url:
            options.append({
                'vendor': 'Cardmarket',
                'currency': 'EUR',
                'price': self._cardmarket_trend,
                'lowPrice': self._cardmarket_low,
                'url': self._cardmarket_url,
            })
        # Always available: real completed-sale comps, which no marketplace shows you.
        set_name = self.card_set._name if self.card_set else ''
        query = f"{self._name} {set_name} {self._number}".strip().replace(' ', '+')
        options.append({
            'vendor': 'eBay (sold)',
            'currency': 'USD',
            'price': None,
            'lowPrice': None,
            'url': f'https://www.ebay.com/sch/i.html?_nkw={query}&LH_Sold=1&LH_Complete=1',
        })
        # Cheapest first; vendors without a price sort last.
        options.sort(key=lambda o: (o['price'] is None, o['price'] or 0))
        return options

    # --- CRUD ---

    def create(self):
        try:
            db.session.add(self)
            db.session.commit()
            return self
        except IntegrityError:
            db.session.rollback()
            return None

    def read(self, include_buy_options=False):
        data = {
            'id': self.id,
            'setId': self._set_id,
            'setName': self.card_set._name if self.card_set else None,
            'name': self._name,
            'number': self._number,
            'rarity': self._rarity,
            'supertype': self._supertype,
            'subtypes': self._subtypes or [],
            'types': self._types or [],
            'artist': self._artist,
            'flavorText': self._flavor_text,
            'imageSmall': self._image_small,
            'imageLarge': self._image_large,
            'marketPrice': self.market_price,
            'prices': {
                'tcgplayer': {
                    'market': self._tcgplayer_market,
                    'low': self._tcgplayer_low,
                    'mid': self._tcgplayer_mid,
                    'high': self._tcgplayer_high,
                },
                'cardmarket': {
                    'trend': self._cardmarket_trend,
                    'low': self._cardmarket_low,
                },
            },
            'priceUpdatedAt': self._price_updated_at.isoformat() if self._price_updated_at else None,
        }
        if include_buy_options:
            data['buyOptions'] = self.buy_options()
        return data

    def update(self, data):
        for key, attr in (('name', '_name'), ('number', '_number'), ('rarity', '_rarity'),
                          ('supertype', '_supertype'), ('subtypes', '_subtypes'),
                          ('types', '_types'), ('artist', '_artist'),
                          ('flavorText', '_flavor_text'),
                          ('imageSmall', '_image_small'), ('imageLarge', '_image_large')):
            if key in data and data[key] is not None:
                setattr(self, attr, data[key])
        if 'tcgplayer' in data or 'cardmarket' in data:
            self.set_prices(data.get('tcgplayer'), data.get('cardmarket'))
        self._synced_at = datetime.utcnow()
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
    def get_by_id(card_id):
        return Card.query.get(card_id)

    @staticmethod
    def search(name=None, set_id=None, rarity=None, card_type=None,
               min_price=None, max_price=None, sort='name', page=1, per_page=48):
        """
        Paginated card search backing the browse grid.

        Returns a Flask-SQLAlchemy Pagination object.
        """
        query = Card.query
        if name:
            query = query.filter(Card._name.ilike(f'%{name}%'))
        if set_id:
            query = query.filter(Card._set_id == set_id)
        if rarity:
            query = query.filter(Card._rarity == rarity)
        if card_type:
            # _types is a JSON array; a LIKE against its text form is portable
            # across SQLite (dev) and MySQL (prod).
            query = query.filter(db.cast(Card._types, db.String).ilike(f'%{card_type}%'))
        if min_price is not None:
            query = query.filter(Card._tcgplayer_market >= min_price)
        if max_price is not None:
            query = query.filter(Card._tcgplayer_market <= max_price)

        if sort == 'price_desc':
            query = query.order_by(Card._tcgplayer_market.desc().nullslast())
        elif sort == 'price_asc':
            query = query.order_by(Card._tcgplayer_market.asc().nullsfirst())
        elif sort == 'number':
            query = query.order_by(Card._number.asc())
        else:
            query = query.order_by(Card._name.asc())

        return query.paginate(page=page, per_page=per_page, error_out=False)

    @staticmethod
    def all_rarities():
        rows = db.session.query(Card._rarity).distinct().all()
        return sorted({r[0] for r in rows if r[0]})

    @staticmethod
    def counts_by_set():
        """
        {set_id: card_count} for every set that has at least one card, in a
        single grouped query. Used to hide empty sets from the UI so a user
        never clicks into a set with nothing in it.
        """
        rows = (db.session.query(Card._set_id, db.func.count(Card.id))
                .group_by(Card._set_id)
                .all())
        return {set_id: count for set_id, count in rows}


class PriceSnapshot(db.Model):
    """
    A daily price reading for one card, used to chart portfolio value over time.

    One row per (card, date) so repeated syncs on the same day overwrite rather
    than accumulate.
    """
    __tablename__ = 'tcg_price_snapshots'
    __table_args__ = (
        db.UniqueConstraint('card_id', 'snapshot_date', name='uq_card_date'),
    )

    id = db.Column(db.Integer, primary_key=True)
    _card_id = db.Column('card_id', db.String(50), db.ForeignKey('tcg_cards.id'), nullable=False)
    _snapshot_date = db.Column('snapshot_date', db.Date, default=date.today, nullable=False)
    _tcgplayer_market = db.Column('tcgplayer_market', db.Float, nullable=True)
    _cardmarket_trend = db.Column('cardmarket_trend', db.Float, nullable=True)

    def __init__(self, card_id, tcgplayer_market=None, cardmarket_trend=None, snapshot_date=None):
        self._card_id = card_id
        self._tcgplayer_market = tcgplayer_market
        self._cardmarket_trend = cardmarket_trend
        self._snapshot_date = snapshot_date or date.today()

    def read(self):
        return {
            'cardId': self._card_id,
            'date': self._snapshot_date.isoformat() if self._snapshot_date else None,
            'tcgplayerMarket': self._tcgplayer_market,
            'cardmarketTrend': self._cardmarket_trend,
        }

    @staticmethod
    def record(card_id, tcgplayer_market=None, cardmarket_trend=None):
        """Upsert today's snapshot for a card."""
        today = date.today()
        existing = PriceSnapshot.query.filter_by(_card_id=card_id, _snapshot_date=today).first()
        if existing:
            existing._tcgplayer_market = tcgplayer_market
            existing._cardmarket_trend = cardmarket_trend
            return existing
        snap = PriceSnapshot(card_id, tcgplayer_market, cardmarket_trend)
        db.session.add(snap)
        return snap

    @staticmethod
    def history(card_id, limit=90):
        """Most recent N snapshots for a card, oldest first (chart order)."""
        rows = (PriceSnapshot.query
                .filter_by(_card_id=card_id)
                .order_by(PriceSnapshot._snapshot_date.desc())
                .limit(limit)
                .all())
        return list(reversed(rows))


def initCatalog():
    """
    Create catalog tables and seed a tiny offline sample.

    Real data arrives via services/pokemontcg_service.py sync_all(). The seed
    exists so a fresh clone renders a populated UI without an API key.
    """
    with app.app_context():
        db.create_all()

        if CardSet.query.first():
            print("✅ TCG catalog already populated, skipping seed")
            return

        base = CardSet(
            id='base1', name='Base', series='Base',
            printed_total=102, total=102, release_date=date(1999, 1, 9),
            logo_url='https://images.pokemontcg.io/base1/logo.png',
            symbol_url='https://images.pokemontcg.io/base1/symbol.png',
            ptcgo_code='BS',
        )
        base.create()

        samples = [
            ('base1-4', 'Charizard', '4', 'Rare Holo', ['Fire'], 384.50, 402.10),
            ('base1-2', 'Blastoise', '2', 'Rare Holo', ['Water'], 149.99, 155.00),
            ('base1-15', 'Venusaur', '15', 'Rare Holo', ['Grass'], 121.25, 118.40),
            ('base1-58', 'Pikachu', '58', 'Common', ['Lightning'], 12.75, 11.90),
        ]
        for cid, name, number, rarity, types, tcg_price, cm_price in samples:
            card = Card(
                id=cid, set_id='base1', name=name, number=number, rarity=rarity,
                supertype='Pokémon', types=types,
                image_small=f'https://images.pokemontcg.io/base1/{number}.png',
                image_large=f'https://images.pokemontcg.io/base1/{number}_hires.png',
            )
            card.set_prices(
                tcgplayer={'market': tcg_price, 'low': round(tcg_price * 0.85, 2),
                           'mid': tcg_price, 'high': round(tcg_price * 1.4, 2),
                           'url': f'https://www.tcgplayer.com/search/pokemon/product?q={name}'},
                cardmarket={'trend': cm_price, 'low': round(cm_price * 0.82, 2),
                            'url': f'https://www.cardmarket.com/en/Pokemon/Products/Search?searchString={name}'},
            )
            if card.create():
                PriceSnapshot.record(cid, tcg_price, cm_price)
                print(f"  Seeded card: {name} (${tcg_price})")

        db.session.commit()
        print("✅ TCG catalog seeded")
