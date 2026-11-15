//! `delta_position`: turn a leader's change of position into the order a follower should send, given
//! the book and a slippage cap. Pure: no clock, no network, no state.
//!
//! The leader's position is a signed count of 1e-8 units. The follower wants `ratio` times the change.
//! The reference price is the best price on the side the follower would take (the ask for a buy, the
//! bid for a sell). The limit price is the reference moved against the follower by the cap in basis
//! points, rounded so the limit never goes beyond the cap. The order's size is what the book can fill
//! at or inside that limit: the follower does not chase the price to get the rest.

use crate::book::{Fixed, Level, OrderBook};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderSide {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FollowConfig {
    /// The follower's size is `ratio_num / ratio_den` of the leader's change.
    pub ratio_num: u64,
    pub ratio_den: u64,
    /// The most the average fill and the limit may be worse than the reference price, in basis points.
    pub max_slippage_bps: u32,
    /// A follower order below this size is not sent.
    pub min_qty: Fixed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FollowerOrder {
    pub side: OrderSide,
    /// What the book can fill inside the cap. Never more than `wanted_qty`.
    pub qty: Fixed,
    /// The limit price to send the order at.
    pub limit_price: Fixed,
    pub reference_price: Fixed,
    /// Volume weighted average price of the levels the order would take, rounded toward the reference.
    pub avg_price: Fixed,
    /// How far the average is from the reference, in basis points rounded up.
    pub slippage_bps: u32,
    /// What the ratio asked for, before the cap.
    pub wanted_qty: Fixed,
    /// `wanted_qty - qty`: the part the cap kept out.
    pub unfilled_qty: Fixed,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FollowError {
    /// The leader's position did not change.
    NoChange,
    /// The ratio or the cap is not usable (zero denominator, or a cap of 100 percent or more).
    BadConfig,
    /// The size after the ratio is below the minimum.
    BelowMinimum { wanted: Fixed },
    /// The side the follower would take is empty.
    EmptyBook,
    /// A number does not fit.
    Overflow,
}

impl std::fmt::Display for FollowError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FollowError::NoChange => write!(f, "the leader's position did not change"),
            FollowError::BadConfig => write!(f, "ratio denominator is zero or the cap is 100 percent or more"),
            FollowError::BelowMinimum { wanted } => write!(f, "wanted size {wanted} is below the minimum"),
            FollowError::EmptyBook => write!(f, "the side to take is empty"),
            FollowError::Overflow => write!(f, "a number does not fit"),
        }
    }
}

impl std::error::Error for FollowError {}

const BPS: u128 = 10_000;

pub fn delta_position(
    leader_before: i64,
    leader_after: i64,
    book: &OrderBook,
    cfg: &FollowConfig,
) -> Result<FollowerOrder, FollowError> {
    if cfg.ratio_den == 0 || cfg.max_slippage_bps as u128 >= BPS {
        return Err(FollowError::BadConfig);
    }
    let change = leader_after as i128 - leader_before as i128;
    let side = match change.signum() {
        1 => OrderSide::Buy,
        -1 => OrderSide::Sell,
        _ => return Err(FollowError::NoChange),
    };
    let wanted_units = (change.unsigned_abs())
        .checked_mul(cfg.ratio_num as u128)
        .ok_or(FollowError::Overflow)?
        / cfg.ratio_den as u128;
    let wanted = Fixed(u64::try_from(wanted_units).map_err(|_| FollowError::Overflow)?);
    if wanted < cfg.min_qty || wanted.is_zero() {
        return Err(FollowError::BelowMinimum { wanted });
    }

    let (best, levels): (Level, Vec<Level>) = match side {
        OrderSide::Buy => {
            let (_, asks) = book.depth(usize::MAX);
            (*asks.first().ok_or(FollowError::EmptyBook)?, asks)
        }
        OrderSide::Sell => {
            let (bids, _) = book.depth(usize::MAX);
            (*bids.first().ok_or(FollowError::EmptyBook)?, bids)
        }
    };
    let reference = best.price;
    let bps = cfg.max_slippage_bps as u128;
    // A buy limit is rounded down and a sell limit rounded up, so each stays inside the cap.
    let limit = match side {
        OrderSide::Buy => {
            let v = (reference.0 as u128) * (BPS + bps) / BPS;
            Fixed(u64::try_from(v).map_err(|_| FollowError::Overflow)?)
        }
        OrderSide::Sell => {
            let num = (reference.0 as u128) * (BPS - bps);
            Fixed(u64::try_from(num.div_ceil(BPS)).map_err(|_| FollowError::Overflow)?)
        }
    };

    let mut remaining = wanted.0 as u128;
    let mut filled: u128 = 0;
    let mut notional: u128 = 0;
    for l in &levels {
        let inside = match side {
            OrderSide::Buy => l.price <= limit,
            OrderSide::Sell => l.price >= limit,
        };
        if !inside || remaining == 0 {
            break;
        }
        let take = remaining.min(l.qty.0 as u128);
        notional = notional
            .checked_add(take * l.price.0 as u128)
            .ok_or(FollowError::Overflow)?;
        filled += take;
        remaining -= take;
    }
    // The best level is always inside its own limit, so something is filled.
    debug_assert!(filled > 0);
    // Round the average toward the reference so it never reads worse than it is.
    let avg = match side {
        OrderSide::Buy => notional / filled,
        OrderSide::Sell => notional.div_ceil(filled),
    };
    let avg = Fixed(avg as u64);
    let diff = match side {
        OrderSide::Buy => avg.0.saturating_sub(reference.0),
        OrderSide::Sell => reference.0.saturating_sub(avg.0),
    } as u128;
    let slip = (diff * BPS).div_ceil(reference.0.max(1) as u128);
    Ok(FollowerOrder {
        side,
        qty: Fixed(filled as u64),
        limit_price: limit,
        reference_price: reference,
        avg_price: avg,
        slippage_bps: slip as u32,
        wanted_qty: wanted,
        unfilled_qty: Fixed(remaining as u64),
    })
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
    fn book() -> OrderBook {
        OrderBook::from_snapshot(
            1,
            &[lv("99.00", "1"), lv("98.00", "2"), lv("90.00", "10")],
            &[lv("100.00", "1"), lv("100.40", "2"), lv("102.00", "10")],
        )
        .unwrap()
    }
    fn cfg(bps: u32) -> FollowConfig {
        FollowConfig {
            ratio_num: 1,
            ratio_den: 1,
            max_slippage_bps: bps,
            min_qty: Fixed(1),
        }
    }
    fn units(s: &str) -> i64 {
        f(s).0 as i64
    }

    #[test]
    fn a_buy_inside_the_top_level_fills_at_the_best_ask() {
        let o = delta_position(0, units("0.5"), &book(), &cfg(50)).unwrap();
        assert_eq!(o.side, OrderSide::Buy);
        assert_eq!(o.qty, f("0.5"));
        assert_eq!(o.reference_price, f("100.00"));
        assert_eq!(o.avg_price, f("100.00"));
        assert_eq!(o.slippage_bps, 0);
        assert_eq!(o.unfilled_qty, Fixed::ZERO);
        assert_eq!(o.limit_price, f("100.50"));
    }

    #[test]
    fn a_buy_that_walks_two_levels_reports_the_average_and_the_slippage() {
        // 1 at 100.00 and 1 at 100.40: average 100.20, 20 bps over the reference, inside a 50 bps cap.
        let o = delta_position(0, units("2"), &book(), &cfg(50)).unwrap();
        assert_eq!(o.qty, f("2"));
        assert_eq!(o.avg_price, f("100.20"));
        assert_eq!(o.slippage_bps, 20);
        assert_eq!(o.unfilled_qty, Fixed::ZERO);
    }

    #[test]
    fn the_cap_keeps_the_far_levels_out_and_the_rest_is_unfilled() {
        // Cap 30 bps: limit 100.30, so the level at 100.40 is outside. Only 1 fills of the 2 wanted.
        let o = delta_position(0, units("2"), &book(), &cfg(30)).unwrap();
        assert_eq!(o.qty, f("1"));
        assert_eq!(o.limit_price, f("100.30"));
        assert_eq!(o.unfilled_qty, f("1"));
        assert_eq!(o.wanted_qty, f("2"));
    }

    #[test]
    fn a_reduction_is_a_sell_against_the_bids() {
        let o = delta_position(units("5"), units("2"), &book(), &cfg(200)).unwrap();
        assert_eq!(o.side, OrderSide::Sell);
        assert_eq!(o.reference_price, f("99.00"));
        // limit 99.00 * 0.98 = 97.02: takes 1 at 99.00 and 2 at 98.00, wants 3, fills all 3.
        assert_eq!(o.limit_price, f("97.02"));
        assert_eq!(o.qty, f("3"));
        // (99 + 2*98) / 3 = 98.333...: rounded up toward the reference
        assert_eq!(o.avg_price, Fixed(9_833_333_334));
        assert!(o.slippage_bps >= 67 && o.slippage_bps <= 68);
    }

    #[test]
    fn a_short_flip_counts_the_whole_signed_change() {
        // Leader goes from long 1 to short 1: change is minus 2, a sell of 2.
        let o = delta_position(units("1"), -units("1"), &book(), &cfg(300)).unwrap();
        assert_eq!(o.side, OrderSide::Sell);
        assert_eq!(o.wanted_qty, f("2"));
    }

    #[test]
    fn the_ratio_scales_the_size_and_floors_it() {
        let c = FollowConfig {
            ratio_num: 1,
            ratio_den: 3,
            max_slippage_bps: 50,
            min_qty: Fixed(1),
        };
        let o = delta_position(0, 10, &book(), &c).unwrap(); // 10 units / 3 = 3 units
        assert_eq!(o.wanted_qty, Fixed(3));
        let c = FollowConfig {
            ratio_num: 5,
            ratio_den: 1,
            ..c
        };
        let o = delta_position(0, units("0.1"), &book(), &c).unwrap();
        assert_eq!(o.wanted_qty, f("0.5"));
    }

    #[test]
    fn no_change_a_dust_size_a_bad_config_and_an_empty_side_are_refused() {
        assert_eq!(delta_position(7, 7, &book(), &cfg(50)), Err(FollowError::NoChange));
        let c = FollowConfig {
            min_qty: f("1"),
            ..cfg(50)
        };
        assert_eq!(
            delta_position(0, units("0.5"), &book(), &c),
            Err(FollowError::BelowMinimum { wanted: f("0.5") })
        );
        let c = FollowConfig {
            ratio_den: 0,
            ..cfg(50)
        };
        assert_eq!(delta_position(0, 5, &book(), &c), Err(FollowError::BadConfig));
        assert_eq!(delta_position(0, 5, &book(), &cfg(10_000)), Err(FollowError::BadConfig));
        let empty_asks = OrderBook::from_snapshot(1, &[lv("1", "1")], &[]).unwrap();
        assert_eq!(delta_position(0, 5, &empty_asks, &cfg(50)), Err(FollowError::EmptyBook));
        // A sell still works on that book.
        assert!(delta_position(5, 0, &empty_asks, &cfg(50)).is_ok());
    }

    #[test]
    fn a_size_equal_to_the_minimum_is_sent() {
        let c = FollowConfig {
            min_qty: f("0.5"),
            ..cfg(50)
        };
        assert!(delta_position(0, units("0.5"), &book(), &c).is_ok());
        assert!(delta_position(0, units("0.49999999"), &book(), &c).is_err());
    }

    #[test]
    fn every_error_says_what_it_is() {
        for (e, word) in [
            (FollowError::NoChange, "did not change"),
            (FollowError::BadConfig, "denominator"),
            (FollowError::BelowMinimum { wanted: Fixed(5) }, "below the minimum"),
            (FollowError::EmptyBook, "empty"),
            (FollowError::Overflow, "fit"),
        ] {
            assert!(e.to_string().contains(word), "{e:?} printed {:?}", e.to_string());
        }
    }

    #[test]
    fn a_sell_limit_is_rounded_up_and_a_buy_limit_down_when_the_cap_does_not_divide_evenly() {
        // 99.00000001 is 9_900_000_001 units. With a 1 bps cap the sell limit is that times 9999 / 10000,
        // which is 9_899_010_000.9999: rounded up to 9_899_010_001 so it never goes past the cap.
        let b = OrderBook::from_snapshot(
            1,
            &[Level {
                price: Fixed(9_900_000_001),
                qty: Fixed(500_000_000),
            }],
            &[lv("101", "1")],
        )
        .unwrap();
        let o = delta_position(units("1"), 0, &b, &cfg(1)).unwrap();
        assert_eq!(o.limit_price, Fixed(9_899_010_001));
        // A buy against an ask of 101.00000001 and a 1 bps cap: times 10001 / 10000 is 10_101_010_001.0001, rounded down.
        let b = OrderBook::from_snapshot(
            1,
            &[lv("99", "1")],
            &[Level {
                price: Fixed(10_100_000_001),
                qty: Fixed(500_000_000),
            }],
        )
        .unwrap();
        let o = delta_position(0, units("1"), &b, &cfg(1)).unwrap();
        assert_eq!(o.limit_price, Fixed(10_101_010_001));
    }

    #[test]
    fn a_zero_cap_fills_only_at_the_reference_price() {
        let o = delta_position(0, units("5"), &book(), &cfg(0)).unwrap();
        assert_eq!(o.limit_price, f("100.00"));
        assert_eq!(o.qty, f("1"));
        assert_eq!(o.unfilled_qty, f("4"));
    }

    #[test]
    fn a_size_that_does_not_fit_overflows_cleanly() {
        let c = FollowConfig {
            ratio_num: u64::MAX,
            ratio_den: 1,
            max_slippage_bps: 50,
            min_qty: Fixed(1),
        };
        assert_eq!(delta_position(0, i64::MAX, &book(), &c), Err(FollowError::Overflow));
    }
}
