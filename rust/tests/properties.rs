//! Property tests: random streams against an independent model, and the rules that must hold for any input.

use std::collections::BTreeMap;

use bookwatch::book::{ApplyError, Delta, Fixed, Level, OrderBook};
use bookwatch::follow::{delta_position, FollowConfig, OrderSide};
use proptest::prelude::*;

const UNIT: u64 = 1_000_000; // one price step in fixed point units

fn lvl(p: u64, q: u64) -> Level {
    Level { price: Fixed(p * UNIT), qty: Fixed(q * UNIT) }
}

/// One update: (is_bid, price step, size). Bids live in steps 1..=50 and asks in 51..=100, so no stream of
/// them can ever cross, and every stream is valid.
fn update() -> impl Strategy<Value = (bool, u64, u64)> {
    (any::<bool>(), 0u64..50, 0u64..6).prop_map(|(is_bid, off, q)| (is_bid, if is_bid { 1 + off } else { 51 + off }, q))
}

fn model_levels(m: &BTreeMap<u64, u64>, descending: bool) -> Vec<Level> {
    let it = m.iter().filter(|(_, q)| **q > 0).map(|(p, q)| lvl(*p, *q));
    if descending {
        it.rev().collect()
    } else {
        it.collect()
    }
}

fn set_model(bids: &mut BTreeMap<u64, u64>, asks: &mut BTreeMap<u64, u64>, (is_bid, p, q): (bool, u64, u64)) {
    let side = if is_bid { bids } else { asks };
    if q == 0 {
        side.remove(&p);
    } else {
        side.insert(p, q);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(512))]

    /// Applying a random valid delta stream gives the same book as a snapshot of the final state.
    #[test]
    fn a_delta_stream_equals_the_final_snapshot(
        initial in proptest::collection::vec(update(), 0..30),
        stream in proptest::collection::vec((proptest::collection::vec(update(), 0..6), 0u64..3), 0..60),
    ) {
        let mut bids: BTreeMap<u64, u64> = BTreeMap::new();
        let mut asks: BTreeMap<u64, u64> = BTreeMap::new();
        for u in &initial {
            set_model(&mut bids, &mut asks, *u);
        }
        let mut book = OrderBook::from_snapshot(1000, &model_levels(&bids, true), &model_levels(&asks, false)).unwrap();

        let mut next = 1001u64;
        for (updates, span) in &stream {
            let mut d = Delta { first_id: next, final_id: next + span, bids: vec![], asks: vec![] };
            for u in updates {
                set_model(&mut bids, &mut asks, *u);
                let l = lvl(u.1, u.2);
                if u.0 {
                    d.bids.push(l)
                } else {
                    d.asks.push(l)
                }
            }
            next += span + 1;
            prop_assert_eq!(book.apply(&d), Ok(()));
        }
        let expected =
            OrderBook::from_snapshot(next - 1, &model_levels(&bids, true), &model_levels(&asks, false)).unwrap();
        prop_assert_eq!(book.all_levels(), expected.all_levels());
        prop_assert_eq!(book.last_update_id(), expected.last_update_id());
        prop_assert_eq!(book.best_bid(), expected.best_bid());
        prop_assert_eq!(book.best_ask(), expected.best_ask());
    }

    /// For any delta at all, even a crossing or out of order one: if it is refused the book is untouched,
    /// and if it is accepted the book is not crossed and sits at the delta's final id.
    #[test]
    fn a_refused_delta_changes_nothing_and_an_accepted_one_never_crosses(
        deltas in proptest::collection::vec(
            (
                0u64..12, 0u64..4,
                proptest::collection::vec((0u64..40, 0u64..4), 0..4),
                proptest::collection::vec((0u64..40, 0u64..4), 0..4),
            ),
            0..40,
        ),
    ) {
        let mut book = OrderBook::from_snapshot(10, &[lvl(18, 1), lvl(15, 1)], &[lvl(22, 1), lvl(25, 1)]).unwrap();
        for (back, span, b, a) in deltas {
            let first = book.last_update_id().saturating_sub(back) + 1; // sometimes old, sometimes right
            let d = Delta {
                first_id: first,
                final_id: first + span,
                bids: b.iter().map(|(p, q)| lvl(*p, *q)).collect(),
                asks: a.iter().map(|(p, q)| lvl(*p, *q)).collect(),
            };
            let before = book.clone();
            match book.apply(&d) {
                Err(e) => {
                    prop_assert_eq!(&book, &before, "refused with {:?} but the book changed", e);
                    if let ApplyError::Crossed { bid, ask } = e {
                        prop_assert!(bid >= ask);
                    }
                }
                Ok(()) => {
                    prop_assert_eq!(book.last_update_id(), d.final_id);
                    if let (Some(b), Some(a)) = (book.best_bid(), book.best_ask()) {
                        prop_assert!(b.price < a.price);
                    }
                }
            }
            let (bids, asks) = book.all_levels();
            prop_assert!(bids.iter().chain(asks.iter()).all(|l| !l.qty.is_zero()));
            prop_assert!(bids.windows(2).all(|w| w[0].price > w[1].price));
            prop_assert!(asks.windows(2).all(|w| w[0].price < w[1].price));
        }
    }

    /// A follower order never goes past the cap, never asks for more than was wanted, and the unfilled part adds up.
    #[test]
    fn a_follower_order_stays_inside_its_cap(
        bid_steps in proptest::collection::btree_map(1u64..50, 1u64..20, 1..8),
        ask_steps in proptest::collection::btree_map(51u64..100, 1u64..20, 1..8),
        before in -50i64..50,
        after in -50i64..50,
        num in 1u64..5,
        den in 1u64..5,
        cap in 0u32..2000,
    ) {
        let bids: Vec<Level> = bid_steps.iter().map(|(p, q)| lvl(*p, *q)).collect();
        let asks: Vec<Level> = ask_steps.iter().map(|(p, q)| lvl(*p, *q)).collect();
        let book = OrderBook::from_snapshot(1, &bids, &asks).unwrap();
        let cfg = FollowConfig { ratio_num: num, ratio_den: den, max_slippage_bps: cap, min_qty: Fixed(1) };
        let (b, a) = (before * UNIT as i64, after * UNIT as i64);
        if let Ok(o) = delta_position(b, a, &book, &cfg) {
            prop_assert!(o.qty.0 > 0);
            prop_assert!(o.qty.0 <= o.wanted_qty.0);
            prop_assert_eq!(o.qty.0 + o.unfilled_qty.0, o.wanted_qty.0);
            prop_assert!(o.slippage_bps <= cap);
            match o.side {
                OrderSide::Buy => {
                    prop_assert!(a > b);
                    prop_assert!(o.avg_price <= o.limit_price);
                    prop_assert!(o.avg_price >= o.reference_price);
                    let allowed = o.reference_price.0 as u128 * (10_000 + cap as u128);
                    prop_assert!(o.limit_price.0 as u128 * 10_000 <= allowed);
                }
                OrderSide::Sell => {
                    prop_assert!(a < b);
                    prop_assert!(o.avg_price >= o.limit_price);
                    prop_assert!(o.avg_price <= o.reference_price);
                    let floor = o.reference_price.0 as u128 * (10_000 - cap as u128);
                    prop_assert!(o.limit_price.0 as u128 * 10_000 >= floor);
                }
            }
            // What the book can fill inside the limit bounds the size, and the order takes all of it up to the wish.
            let (bs, asks_v) = book.all_levels();
            let avail: u128 = match o.side {
                OrderSide::Buy => asks_v.iter().filter(|l| l.price <= o.limit_price).map(|l| l.qty.0 as u128).sum(),
                OrderSide::Sell => bs.iter().filter(|l| l.price >= o.limit_price).map(|l| l.qty.0 as u128).sum(),
            };
            prop_assert_eq!(o.qty.0 as u128, avail.min(o.wanted_qty.0 as u128));
        }
    }
}
