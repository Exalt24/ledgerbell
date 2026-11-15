//! The local order book: integer prices and sizes, a delta rule that refuses what cannot be right.
//!
//! Prices and sizes are fixed point integers with eight decimals (`Fixed`), so there is no float
//! anywhere in the book. A side is a `BTreeMap` from price to size, which keeps the best level at one end.

use std::collections::BTreeMap;
use std::fmt;

/// Eight decimal places, the precision Binance quotes spot prices and sizes at.
pub const SCALE: u64 = 100_000_000;

/// A non negative decimal number as an integer count of 1e-8 units.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Fixed(pub u64);

impl Fixed {
    pub const ZERO: Fixed = Fixed(0);

    /// Parses a decimal string such as "0.00100000" or "42". More than eight decimals is refused
    /// unless the extra digits are zeros, so no value is silently rounded.
    pub fn parse(s: &str) -> Result<Fixed, ParseError> {
        let bad = || ParseError(s.to_string());
        if s.is_empty() {
            return Err(bad());
        }
        let (int_part, frac_part) = match s.split_once('.') {
            Some((i, f)) => (i, f),
            None => (s, ""),
        };
        if int_part.is_empty() && frac_part.is_empty() {
            return Err(bad());
        }
        if !int_part.bytes().all(|b| b.is_ascii_digit()) || !frac_part.bytes().all(|b| b.is_ascii_digit()) {
            return Err(bad());
        }
        let (kept, extra) = if frac_part.len() > 8 { frac_part.split_at(8) } else { (frac_part, "") };
        if extra.bytes().any(|b| b != b'0') {
            return Err(bad());
        }
        let whole: u64 = if int_part.is_empty() { 0 } else { int_part.parse().map_err(|_| bad())? };
        let mut frac: u64 = if kept.is_empty() { 0 } else { kept.parse().map_err(|_| bad())? };
        for _ in kept.len()..8 {
            frac *= 10;
        }
        let units = whole.checked_mul(SCALE).and_then(|w| w.checked_add(frac)).ok_or_else(bad)?;
        Ok(Fixed(units))
    }

    pub fn is_zero(self) -> bool {
        self.0 == 0
    }
}

impl fmt::Display for Fixed {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{:08}", self.0 / SCALE, self.0 % SCALE)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError(pub String);

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "not a decimal with at most eight places: {:?}", self.0)
    }
}

impl std::error::Error for ParseError {}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Level {
    pub price: Fixed,
    pub qty: Fixed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Side {
    Bid,
    Ask,
}

/// One incremental update: the book changes from `first_id - 1` to `final_id`. A level with a zero
/// size means "remove this level".
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct Delta {
    pub first_id: u64,
    pub final_id: u64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApplyError {
    /// The delta is entirely at or below the book's update id: already applied, or old. Drop it.
    Stale { book_id: u64, delta_final: u64 },
    /// An update id is missing between the book and this delta. The book must be rebuilt.
    Gap { expected_first: u64, got_first: u64 },
    /// The delta starts at or below the book's id but ends above it, after the first delta. It
    /// cannot follow a delta that ended at the book's id.
    Overlap { book_id: u64, delta_first: u64, delta_final: u64 },
    /// `first_id` is above `final_id`.
    Malformed { first_id: u64, final_id: u64 },
    /// Applying the delta would leave the best bid at or above the best ask. The book is unchanged.
    Crossed { bid: Fixed, ask: Fixed },
}

impl fmt::Display for ApplyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApplyError::Stale { book_id, delta_final } => {
                write!(f, "stale delta ending at {delta_final}, book is at {book_id}")
            }
            ApplyError::Gap { expected_first, got_first } => {
                write!(f, "gap: expected a delta starting at {expected_first}, got {got_first}")
            }
            ApplyError::Overlap { book_id, delta_first, delta_final } => {
                write!(f, "overlap: delta {delta_first}..{delta_final} straddles the book id {book_id}")
            }
            ApplyError::Malformed { first_id, final_id } => {
                write!(f, "malformed delta: first {first_id} is above final {final_id}")
            }
            ApplyError::Crossed { bid, ask } => write!(f, "crossed book: bid {bid} is not below ask {ask}"),
        }
    }
}

impl std::error::Error for ApplyError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SnapshotError {
    Crossed { bid: Fixed, ask: Fixed },
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            SnapshotError::Crossed { bid, ask } => write!(f, "snapshot is crossed: bid {bid}, ask {ask}"),
        }
    }
}

impl std::error::Error for SnapshotError {}

/// How a delta relates to the book, which decides what the rule is for its ids.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// Right after a snapshot: the first delta may overlap the snapshot (first <= id + 1 <= final).
    AfterSnapshot,
    /// Live: each delta must start exactly one above the previous one's final id.
    Live,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OrderBook {
    bids: BTreeMap<Fixed, Fixed>,
    asks: BTreeMap<Fixed, Fixed>,
    last_id: u64,
    phase: Phase,
}

impl OrderBook {
    /// Builds a book from a snapshot. Zero size levels are dropped. A crossed snapshot is refused.
    pub fn from_snapshot(last_update_id: u64, bids: &[Level], asks: &[Level]) -> Result<OrderBook, SnapshotError> {
        let mut book = OrderBook {
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
            last_id: last_update_id,
            phase: Phase::AfterSnapshot,
        };
        for l in bids {
            set_level(&mut book.bids, *l);
        }
        for l in asks {
            set_level(&mut book.asks, *l);
        }
        if let Some((bid, ask)) = book.crossed() {
            return Err(SnapshotError::Crossed { bid, ask });
        }
        Ok(book)
    }

    pub fn last_update_id(&self) -> u64 {
        self.last_id
    }

    pub fn best_bid(&self) -> Option<Level> {
        self.bids.iter().next_back().map(|(p, q)| Level { price: *p, qty: *q })
    }

    pub fn best_ask(&self) -> Option<Level> {
        self.asks.iter().next().map(|(p, q)| Level { price: *p, qty: *q })
    }

    pub fn bid_levels(&self) -> usize {
        self.bids.len()
    }

    pub fn ask_levels(&self) -> usize {
        self.asks.len()
    }

    /// The best `n` levels per side, bids from the highest price down and asks from the lowest up.
    pub fn depth(&self, n: usize) -> (Vec<Level>, Vec<Level>) {
        let bids = self.bids.iter().rev().take(n).map(|(p, q)| Level { price: *p, qty: *q }).collect();
        let asks = self.asks.iter().take(n).map(|(p, q)| Level { price: *p, qty: *q }).collect();
        (bids, asks)
    }

    /// Every level, for comparing two books.
    pub fn all_levels(&self) -> (Vec<Level>, Vec<Level>) {
        self.depth(usize::MAX)
    }

    pub fn spread(&self) -> Option<Fixed> {
        match (self.best_bid(), self.best_ask()) {
            (Some(b), Some(a)) => Some(Fixed(a.price.0 - b.price.0)),
            _ => None,
        }
    }

    fn crossed(&self) -> Option<(Fixed, Fixed)> {
        match (self.best_bid(), self.best_ask()) {
            (Some(b), Some(a)) if b.price >= a.price => Some((b.price, a.price)),
            _ => None,
        }
    }

    /// Applies one delta. On any error the book is exactly as it was before the call.
    pub fn apply(&mut self, d: &Delta) -> Result<(), ApplyError> {
        if d.first_id > d.final_id {
            return Err(ApplyError::Malformed { first_id: d.first_id, final_id: d.final_id });
        }
        if d.final_id <= self.last_id {
            return Err(ApplyError::Stale { book_id: self.last_id, delta_final: d.final_id });
        }
        let next = self.last_id + 1;
        match self.phase {
            Phase::AfterSnapshot => {
                if d.first_id > next {
                    return Err(ApplyError::Gap { expected_first: next, got_first: d.first_id });
                }
            }
            Phase::Live => {
                if d.first_id > next {
                    return Err(ApplyError::Gap { expected_first: next, got_first: d.first_id });
                }
                if d.first_id < next {
                    return Err(ApplyError::Overlap {
                        book_id: self.last_id,
                        delta_first: d.first_id,
                        delta_final: d.final_id,
                    });
                }
            }
        }

        // Apply, remembering what each touched level held, so a crossed result can be undone.
        let mut undo: Vec<(Side, Fixed, Option<Fixed>)> = Vec::with_capacity(d.bids.len() + d.asks.len());
        for l in &d.bids {
            undo.push((Side::Bid, l.price, set_level(&mut self.bids, *l)));
        }
        for l in &d.asks {
            undo.push((Side::Ask, l.price, set_level(&mut self.asks, *l)));
        }
        if let Some((bid, ask)) = self.crossed() {
            for (side, price, old) in undo.into_iter().rev() {
                let map = match side {
                    Side::Bid => &mut self.bids,
                    Side::Ask => &mut self.asks,
                };
                match old {
                    Some(q) => {
                        map.insert(price, q);
                    }
                    None => {
                        map.remove(&price);
                    }
                }
            }
            return Err(ApplyError::Crossed { bid, ask });
        }
        self.last_id = d.final_id;
        self.phase = Phase::Live;
        Ok(())
    }
}

/// Sets a level and returns what it held before. A zero size removes the level.
fn set_level(map: &mut BTreeMap<Fixed, Fixed>, l: Level) -> Option<Fixed> {
    if l.qty.is_zero() {
        map.remove(&l.price)
    } else {
        map.insert(l.price, l.qty)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(s: &str) -> Fixed {
        Fixed::parse(s).unwrap()
    }
    fn lv(p: &str, q: &str) -> Level {
        Level { price: f(p), qty: f(q) }
    }
    fn base() -> OrderBook {
        OrderBook::from_snapshot(
            100,
            &[lv("99.00", "1"), lv("98.50", "2"), lv("98.00", "3")],
            &[lv("100.00", "1"), lv("100.50", "2"), lv("101.00", "3")],
        )
        .unwrap()
    }
    fn delta(first: u64, last: u64, bids: Vec<Level>, asks: Vec<Level>) -> Delta {
        Delta { first_id: first, final_id: last, bids, asks }
    }

    #[test]
    fn parses_decimals_exactly() {
        assert_eq!(f("0.00100000"), Fixed(100_000));
        assert_eq!(f("42"), Fixed(42 * SCALE));
        assert_eq!(f("0.1"), Fixed(10_000_000));
        assert_eq!(f("1.123456780"), Fixed(112_345_678));
        assert!(Fixed::parse("1.123456789").is_err());
        assert!(Fixed::parse("").is_err());
        assert!(Fixed::parse("-1").is_err());
        assert!(Fixed::parse("1e3").is_err());
        assert!(Fixed::parse("99999999999999999999").is_err());
        assert_eq!(f("12.5").to_string(), "12.50000000");
    }

    #[test]
    fn best_levels_and_depth_view() {
        let b = base();
        assert_eq!(b.best_bid().unwrap(), lv("99.00", "1"));
        assert_eq!(b.best_ask().unwrap(), lv("100.00", "1"));
        let (bids, asks) = b.depth(2);
        assert_eq!(bids, vec![lv("99.00", "1"), lv("98.50", "2")]);
        assert_eq!(asks, vec![lv("100.00", "1"), lv("100.50", "2")]);
        assert_eq!(b.depth(0), (vec![], vec![]));
        assert_eq!(b.depth(50).0.len(), 3);
        assert_eq!(b.spread().unwrap(), f("1.00"));
    }

    #[test]
    fn crossed_snapshot_is_refused() {
        let r = OrderBook::from_snapshot(1, &[lv("100", "1")], &[lv("100", "1")]);
        assert!(matches!(r, Err(SnapshotError::Crossed { .. })));
    }

    #[test]
    fn crossed_delta_is_refused_and_leaves_the_book_untouched() {
        let mut b = base();
        let before = b.clone();
        // A bid at 100.25 crosses the best ask at 100.00, and the same delta also removes a level
        // that must come back when the delta is undone.
        let d = delta(101, 101, vec![lv("100.25", "1"), lv("98.00", "0")], vec![lv("100.50", "7")]);
        let err = b.apply(&d).unwrap_err();
        assert!(matches!(err, ApplyError::Crossed { .. }));
        assert_eq!(b, before);
        assert_eq!(b.last_update_id(), 100);
    }

    #[test]
    fn zero_size_removes_a_level_and_a_new_best_appears() {
        let mut b = base();
        b.apply(&delta(101, 101, vec![lv("99.00", "0")], vec![])).unwrap();
        assert_eq!(b.best_bid().unwrap(), lv("98.50", "2"));
        assert_eq!(b.bid_levels(), 2);
        // Removing a level that is not there changes nothing and is not an error.
        b.apply(&delta(102, 102, vec![lv("50.00", "0")], vec![])).unwrap();
        assert_eq!(b.bid_levels(), 2);
    }

    #[test]
    fn a_size_replaces_it_does_not_add() {
        let mut b = base();
        b.apply(&delta(101, 101, vec![lv("99.00", "9")], vec![lv("100.00", "4")])).unwrap();
        assert_eq!(b.best_bid().unwrap().qty, f("9"));
        assert_eq!(b.best_ask().unwrap().qty, f("4"));
    }

    #[test]
    fn a_stale_delta_is_refused_and_changes_nothing() {
        let mut b = base();
        let before = b.clone();
        let err = b.apply(&delta(90, 100, vec![lv("99.00", "5")], vec![])).unwrap_err();
        assert_eq!(err, ApplyError::Stale { book_id: 100, delta_final: 100 });
        assert_eq!(b, before);
    }

    #[test]
    fn an_out_of_order_delta_after_a_newer_one_is_refused() {
        let mut b = base();
        b.apply(&delta(101, 103, vec![lv("99.00", "5")], vec![])).unwrap();
        let before = b.clone();
        // Delta 101..102 arrives after 101..103 already applied: entirely old.
        let err = b.apply(&delta(101, 102, vec![lv("99.00", "1")], vec![])).unwrap_err();
        assert!(matches!(err, ApplyError::Stale { .. }));
        assert_eq!(b, before);
    }

    #[test]
    fn a_live_delta_that_straddles_the_book_id_is_refused() {
        let mut b = base();
        b.apply(&delta(101, 103, vec![lv("99.00", "5")], vec![])).unwrap();
        let before = b.clone();
        let err = b.apply(&delta(103, 105, vec![lv("99.00", "1")], vec![])).unwrap_err();
        assert_eq!(err, ApplyError::Overlap { book_id: 103, delta_first: 103, delta_final: 105 });
        assert_eq!(b, before);
    }

    #[test]
    fn a_gap_is_refused_and_names_what_was_expected() {
        let mut b = base();
        let err = b.apply(&delta(103, 103, vec![lv("99.00", "5")], vec![])).unwrap_err();
        assert_eq!(err, ApplyError::Gap { expected_first: 101, got_first: 103 });
        b.apply(&delta(101, 101, vec![], vec![])).unwrap();
        let err = b.apply(&delta(103, 104, vec![], vec![])).unwrap_err();
        assert_eq!(err, ApplyError::Gap { expected_first: 102, got_first: 103 });
    }

    #[test]
    fn the_first_delta_after_a_snapshot_may_overlap_it() {
        let mut b = base();
        // first 95 <= 101 <= final 104: allowed once, right after a snapshot.
        b.apply(&delta(95, 104, vec![lv("99.00", "5")], vec![])).unwrap();
        assert_eq!(b.last_update_id(), 104);
        // From now on the rule is strict.
        let err = b.apply(&delta(100, 106, vec![], vec![])).unwrap_err();
        assert!(matches!(err, ApplyError::Overlap { .. }));
    }

    #[test]
    fn a_malformed_delta_is_refused() {
        let mut b = base();
        let err = b.apply(&delta(105, 101, vec![], vec![])).unwrap_err();
        assert_eq!(err, ApplyError::Malformed { first_id: 105, final_id: 101 });
    }

    #[test]
    fn an_empty_delta_still_advances_the_id() {
        let mut b = base();
        b.apply(&delta(101, 110, vec![], vec![])).unwrap();
        assert_eq!(b.last_update_id(), 110);
    }

    #[test]
    fn a_book_with_one_empty_side_is_not_crossed() {
        let mut b = OrderBook::from_snapshot(1, &[lv("10", "1")], &[]).unwrap();
        assert_eq!(b.best_ask(), None);
        assert_eq!(b.spread(), None);
        b.apply(&delta(2, 2, vec![lv("500", "1")], vec![])).unwrap();
        assert_eq!(b.best_bid().unwrap().price, f("500"));
    }

    #[test]
    fn a_delta_that_crosses_inside_itself_but_not_at_the_end_is_allowed() {
        let mut b = base();
        // The ask at 100.00 is removed in the same delta that adds a bid at 100.25: whole delta is applied
        // atomically, and the final book is not crossed (best ask is now 100.50).
        b.apply(&delta(101, 101, vec![lv("100.25", "1")], vec![lv("100.00", "0")])).unwrap();
        assert_eq!(b.best_bid().unwrap().price, f("100.25"));
        assert_eq!(b.best_ask().unwrap().price, f("100.50"));
    }
}
