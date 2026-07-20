"""
TCG Collect — Collection & Want List Models

Tables: tcg_collection_items, tcg_wantlist_items

This is where TCG Collect diverges from a marketplace. TCGplayer knows what a card
costs; it does not know or care what *you* own. We store cost basis alongside each
owned copy, which turns a card list into a portfolio: unrealized gain/loss, set
completion percentage, and a want list that can be carried into a card show.
"""
from datetime import datetime, date

from sqlalchemy.exc import IntegrityError

from __init__ import app, db
from model.tcg import Card, CardSet


# Condition grades, worst to best. Ordering matters for the UI dropdown.
CONDITIONS = ['Damaged', 'Heavily Played', 'Moderately Played',
              'Lightly Played', 'Near Mint', 'Mint']


def _parse_iso_date(value):
    """Parse an ISO date string from a backup. Returns None if unusable."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


class CollectionItem(db.Model):
    """
    One owned lot of a card: N copies in a given condition/finish.

    A user may hold several rows for the same card -- e.g. 2 Near Mint raw copies
    bought at $310, plus 1 PSA 9 bought at $900 -- because cost basis and grade
    differ per lot. Quantity collapses identical lots.

    Fields:
        _user_id: FK to users.id (owner)
        _card_id: FK to tcg_cards.id
        _quantity: copies in this lot
        _condition: one of CONDITIONS
        _is_foil: holo/reverse-holo variant
        _purchase_price: per-copy cost basis in USD (None = unknown/gift)
        _acquired_date: when it entered the collection
        _grader / _grade: e.g. "PSA" / 9.0 for slabbed cards
        _notes: freeform
    """
    __tablename__ = 'tcg_collection_items'

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    _card_id = db.Column('card_id', db.String(50), db.ForeignKey('tcg_cards.id'), nullable=False, index=True)
    _quantity = db.Column('quantity', db.Integer, default=1, nullable=False)
    _condition = db.Column('condition', db.String(30), default='Near Mint')
    _is_foil = db.Column('is_foil', db.Boolean, default=False)
    _purchase_price = db.Column('purchase_price', db.Float, nullable=True)
    _acquired_date = db.Column('acquired_date', db.Date, default=date.today)
    _grader = db.Column('grader', db.String(20), nullable=True)
    _grade = db.Column('grade', db.Float, nullable=True)
    _notes = db.Column('notes', db.Text, nullable=True)
    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)
    _updated_at = db.Column('updated_at', db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    card = db.relationship('Card', backref='collection_items', lazy=True)
    user = db.relationship('User', backref='collection_items', lazy=True)

    def __init__(self, user_id, card_id, quantity=1, condition='Near Mint',
                 is_foil=False, purchase_price=None, acquired_date=None,
                 grader=None, grade=None, notes=None):
        self._user_id = user_id
        self._card_id = card_id
        self._quantity = quantity
        self._condition = condition
        self._is_foil = is_foil
        self._purchase_price = purchase_price
        self._acquired_date = acquired_date or date.today()
        self._grader = grader
        self._grade = grade
        self._notes = notes

    # --- Portfolio math ---

    @property
    def cost_basis(self):
        """Total paid for this lot. None if purchase price was never recorded."""
        if self._purchase_price is None:
            return None
        return round(self._purchase_price * self._quantity, 2)

    @property
    def market_value(self):
        """Current total market value of this lot."""
        if not self.card or self.card.market_price is None:
            return None
        return round(self.card.market_price * self._quantity, 2)

    @property
    def gain_loss(self):
        """Unrealized gain/loss in USD, or None if either side is unknown."""
        basis, value = self.cost_basis, self.market_value
        if basis is None or value is None:
            return None
        return round(value - basis, 2)

    @property
    def gain_loss_percent(self):
        basis, gl = self.cost_basis, self.gain_loss
        if not basis or gl is None:
            return None
        return round((gl / basis) * 100, 1)

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
            'userId': self._user_id,
            'cardId': self._card_id,
            'card': self.card.read() if self.card else None,
            'quantity': self._quantity,
            'condition': self._condition,
            'isFoil': self._is_foil,
            'purchasePrice': self._purchase_price,
            'acquiredDate': self._acquired_date.isoformat() if self._acquired_date else None,
            'grader': self._grader,
            'grade': self._grade,
            'notes': self._notes,
            'costBasis': self.cost_basis,
            'marketValue': self.market_value,
            'gainLoss': self.gain_loss,
            'gainLossPercent': self.gain_loss_percent,
        }

    def update(self, data):
        for key, attr in (('quantity', '_quantity'), ('condition', '_condition'),
                          ('isFoil', '_is_foil'), ('purchasePrice', '_purchase_price'),
                          ('grader', '_grader'), ('grade', '_grade'), ('notes', '_notes')):
            if key in data:
                setattr(self, attr, data[key])
        if data.get('acquiredDate'):
            self._acquired_date = data['acquiredDate']
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
    def get_by_id(item_id):
        return CollectionItem.query.get(item_id)

    @staticmethod
    def for_user(user_id, set_id=None):
        query = CollectionItem.query.filter_by(_user_id=user_id)
        if set_id:
            query = query.join(Card).filter(Card._set_id == set_id)
        return query.order_by(CollectionItem._created_at.desc()).all()

    @staticmethod
    def portfolio_summary(user_id):
        """
        Headline numbers for the collection dashboard.

        Cards with unknown cost basis are counted in market value but excluded
        from gain/loss, so the percentage stays honest rather than treating
        unknown basis as $0 profit.
        """
        items = CollectionItem.for_user(user_id)
        total_cards = sum(i._quantity for i in items)
        market_value = sum(i.market_value or 0 for i in items)
        priced = [i for i in items if i.cost_basis is not None and i.market_value is not None]
        cost_basis = sum(i.cost_basis for i in priced)
        gain_loss = round(sum(i.gain_loss for i in priced), 2)

        best = max(priced, key=lambda i: i.gain_loss, default=None)
        worst = min(priced, key=lambda i: i.gain_loss, default=None)

        return {
            'uniqueCards': len(items),
            'totalCards': total_cards,
            'marketValue': round(market_value, 2),
            'costBasis': round(cost_basis, 2),
            'gainLoss': gain_loss,
            'gainLossPercent': round((gain_loss / cost_basis) * 100, 1) if cost_basis else None,
            'untrackedBasisCount': len(items) - len(priced),
            'bestPerformer': best.read() if best else None,
            'worstPerformer': worst.read() if worst else None,
        }

    @staticmethod
    def restore(data):
        """
        Rebuild collection rows from a backup produced by read().

        Keyed on (user_id, card_id, condition, is_foil) rather than the primary
        key, because ids are not stable across a database rebuild but that tuple
        identifies a lot uniquely.
        """
        existing = {
            (i._user_id, i._card_id, i._condition, i._is_foil): i
            for i in CollectionItem.query.all()
        }

        for row in data:
            key = (row.get('userId'), row.get('cardId'),
                   row.get('condition'), row.get('isFoil'))
            item = existing.pop(key, None)
            if item:
                item.update(row)
                continue

            # Skip rows whose card is not in the catalog yet -- run
            # `flask tcg sync-all` before restoring.
            if not row.get('userId') or not Card.query.get(row.get('cardId')):
                continue

            CollectionItem(
                user_id=row['userId'],
                card_id=row['cardId'],
                quantity=row.get('quantity', 1),
                condition=row.get('condition', 'Near Mint'),
                is_foil=row.get('isFoil', False),
                purchase_price=row.get('purchasePrice'),
                acquired_date=_parse_iso_date(row.get('acquiredDate')),
                grader=row.get('grader'),
                grade=row.get('grade'),
                notes=row.get('notes'),
            ).create()

        db.session.commit()

    @staticmethod
    def set_completion(user_id, set_id):
        """
        How far through a set the user is, plus exactly which cards are missing.

        This is the collector's actual goal, and neither TCGplayer nor a plain
        price tracker answers it.
        """
        card_set = CardSet.get_by_id(set_id)
        if not card_set:
            return None

        all_cards = card_set.cards.all()
        owned_ids = {i._card_id for i in CollectionItem.for_user(user_id, set_id=set_id)}
        missing = [c for c in all_cards if c.id not in owned_ids]
        total = len(all_cards)

        return {
            'set': card_set.read(),
            'owned': total - len(missing),
            'total': total,
            'percent': round(((total - len(missing)) / total) * 100, 1) if total else 0,
            'missingCount': len(missing),
            # Cheapest gaps first -- the practical way to close out a set.
            'missing': [c.read(include_buy_options=True) for c in
                        sorted(missing, key=lambda c: (c.market_price is None, c.market_price or 0))],
            'costToComplete': round(sum(c.market_price or 0 for c in missing), 2),
        }


class WantListItem(db.Model):
    """
    A card the user is hunting for, with the ceiling price they'll pay.

    Powers "show mode": at a card show you open your want list and every card has
    a live market price and your own max-bid next to it, so a booth asking $80 for
    a $30 card is obvious before you hand over cash.

    Fields:
        _max_price: user's personal ceiling; drives the good-deal check
        _priority: 1 (highest) .. 5 (lowest), for sorting a hunt list
    """
    __tablename__ = 'tcg_wantlist_items'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'card_id', name='uq_user_want_card'),
    )

    id = db.Column(db.Integer, primary_key=True)
    _user_id = db.Column('user_id', db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    _card_id = db.Column('card_id', db.String(50), db.ForeignKey('tcg_cards.id'), nullable=False)
    _max_price = db.Column('max_price', db.Float, nullable=True)
    _priority = db.Column('priority', db.Integer, default=3)
    _notes = db.Column('notes', db.Text, nullable=True)
    _created_at = db.Column('created_at', db.DateTime, default=datetime.utcnow)

    card = db.relationship('Card', backref='wantlist_items', lazy=True)

    def __init__(self, user_id, card_id, max_price=None, priority=3, notes=None):
        self._user_id = user_id
        self._card_id = card_id
        self._max_price = max_price
        self._priority = priority
        self._notes = notes

    @property
    def is_good_deal(self):
        """
        True when market has fallen to or below the user's ceiling.

        None when we lack either number -- deliberately not False, so the UI can
        distinguish "not a deal" from "we don't know yet".
        """
        if self._max_price is None or not self.card or self.card.market_price is None:
            return None
        return self.card.market_price <= self._max_price

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
            'userId': self._user_id,
            'cardId': self._card_id,
            'card': self.card.read(include_buy_options=True) if self.card else None,
            'maxPrice': self._max_price,
            'priority': self._priority,
            'notes': self._notes,
            'marketPrice': self.card.market_price if self.card else None,
            'isGoodDeal': self.is_good_deal,
        }

    def update(self, data):
        for key, attr in (('maxPrice', '_max_price'), ('priority', '_priority'),
                          ('notes', '_notes')):
            if key in data:
                setattr(self, attr, data[key])
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

    @staticmethod
    def get_by_id(item_id):
        return WantListItem.query.get(item_id)

    @staticmethod
    def for_user(user_id):
        return (WantListItem.query
                .filter_by(_user_id=user_id)
                .order_by(WantListItem._priority.asc(), WantListItem._created_at.desc())
                .all())

    @staticmethod
    def restore(data):
        """Rebuild want-list rows from a backup. Keyed on (user_id, card_id)."""
        existing = {
            (i._user_id, i._card_id): i for i in WantListItem.query.all()
        }

        for row in data:
            key = (row.get('userId'), row.get('cardId'))
            item = existing.pop(key, None)
            if item:
                item.update(row)
                continue

            if not row.get('userId') or not Card.query.get(row.get('cardId')):
                continue

            WantListItem(
                user_id=row['userId'],
                card_id=row['cardId'],
                max_price=row.get('maxPrice'),
                priority=row.get('priority', 3),
                notes=row.get('notes'),
            ).create()

        db.session.commit()

    @staticmethod
    def budget_plan(user_id, budget):
        """
        Given a show budget, pick the most want-list cards that fit inside it.

        Greedy cheapest-first: for a collector the goal is usually "close as many
        gaps as possible today", not "buy the single best card".
        """
        items = [i for i in WantListItem.for_user(user_id)
                 if i.card and i.card.market_price is not None]
        items.sort(key=lambda i: i.card.market_price)

        affordable, spent = [], 0.0
        for item in items:
            price = item.card.market_price
            if spent + price <= budget:
                affordable.append(item.read())
                spent += price

        return {
            'budget': budget,
            'plannedSpend': round(spent, 2),
            'remaining': round(budget - spent, 2),
            'cardsCovered': len(affordable),
            'wantListSize': len(items),
            'picks': affordable,
        }


def initCollection():
    """Create collection tables. No seed -- collections are per-user."""
    with app.app_context():
        db.create_all()
        print("✅ TCG collection tables ready")
