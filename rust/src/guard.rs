//! Execution protection, as pure functions and small state machines. Nothing here reads a clock or the
//! network: time is a `u64` count of milliseconds that the caller passes in, so every rule can be tested
//! by writing the numbers down.
//!
//! * [`SlippageLadder`]: how far from the best price an order may reach, widened by a thin book and by
//!   recent misses, tightened by a deep book and by clean fills, always inside fixed bounds.
//! * [`momentum_check`]: refuse to follow when the book has already moved against the follower since the
//!   leader's trade.
//! * [`FillStabilizer`]: a fill counts after it has stayed unchanged for a window.
//! * [`CircuitBreaker`]: closed, open and half open, with a failure count and a cooldown.

use crate::book::Fixed;
use crate::follow::OrderSide;

// ---------------------------------------------------------------- slippage ladder

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SlippageLadder {
    /// Where the ladder starts, in basis points.
    pub base_bps: u32,
    /// The allowed distance never goes below this.
    pub min_bps: u32,
    /// The allowed distance never goes above this.
    pub max_bps: u32,
    /// Added once per recent miss (up to `max_steps` of them).
    pub miss_step_bps: u32,
    /// Taken off once per recent clean fill (up to `max_steps` of them).
    pub fill_step_bps: u32,
    /// How many misses or fills count at most.
    pub max_steps: u32,
    /// Added when the book near the price holds less than twice the wanted size.
    pub thin_step_bps: u32,
    /// Taken off when the book near the price holds at least four times the wanted size.
    pub deep_step_bps: u32,
}

/// What the ladder looks at.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LadderInput {
    /// Size resting on the side the order would take, within the widest distance the ladder allows.
    pub depth: Fixed,
    /// The size the follower wants.
    pub wanted: Fixed,
    /// Orders in a row (most recent first) that did not fill as asked.
    pub recent_misses: u32,
    /// Orders in a row that filled as asked.
    pub recent_fills: u32,
}

impl SlippageLadder {
    pub fn allowed_bps(&self, i: &LadderInput) -> u32 {
        let lo = self.min_bps.min(self.max_bps) as i64;
        let hi = self.max_bps.max(self.min_bps) as i64;
        let misses = i.recent_misses.min(self.max_steps) as i64;
        let fills = i.recent_fills.min(self.max_steps) as i64;
        let wanted = i.wanted.0 as u128;
        let depth = i.depth.0 as u128;
        let mut bps = self.base_bps as i64;
        bps += misses * self.miss_step_bps as i64;
        bps -= fills * self.fill_step_bps as i64;
        if depth < wanted * 2 {
            bps += self.thin_step_bps as i64;
        } else if depth >= wanted * 4 {
            bps -= self.deep_step_bps as i64;
        }
        bps.clamp(lo, hi) as u32
    }
}

// ---------------------------------------------------------------- momentum filter

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MomentumRefusal {
    /// How far the price moved against the follower since the leader traded, in basis points rounded up.
    pub moved_bps: u32,
    pub max_bps: u32,
}

/// `leader_price` is where the leader traded; `current` is the best price the follower would take now
/// (the ask for a buy, the bid for a sell). A move in the follower's favour is never refused.
pub fn momentum_check(
    side: OrderSide,
    leader_price: Fixed,
    current: Fixed,
    max_adverse_bps: u32,
) -> Result<(), MomentumRefusal> {
    if leader_price.is_zero() {
        return Ok(());
    }
    let adverse = match side {
        OrderSide::Buy => current.0.saturating_sub(leader_price.0),
        OrderSide::Sell => leader_price.0.saturating_sub(current.0),
    } as u128;
    let moved = (adverse * 10_000).div_ceil(leader_price.0 as u128);
    let moved = moved.min(u32::MAX as u128) as u32;
    if moved > max_adverse_bps {
        Err(MomentumRefusal {
            moved_bps: moved,
            max_bps: max_adverse_bps,
        })
    } else {
        Ok(())
    }
}

// ---------------------------------------------------------------- fill stabilization

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FillState {
    /// No reading yet, or the last reading changed less than a window ago.
    Pending,
    /// The matched size has been the same for the whole window.
    Stable(Fixed),
}

#[derive(Clone, Copy, Debug)]
pub struct FillStabilizer {
    window_ms: u64,
    current: Option<(Fixed, u64)>,
}

impl FillStabilizer {
    pub fn new(window_ms: u64) -> Self {
        FillStabilizer {
            window_ms,
            current: None,
        }
    }

    /// Feed one reading of the matched size taken at `now_ms`. Time must not go backwards.
    pub fn observe(&mut self, now_ms: u64, matched: Fixed) -> FillState {
        match self.current {
            Some((q, since)) if q == matched => {
                if now_ms.saturating_sub(since) >= self.window_ms {
                    FillState::Stable(q)
                } else {
                    FillState::Pending
                }
            }
            _ => {
                self.current = Some((matched, now_ms));
                if self.window_ms == 0 {
                    FillState::Stable(matched)
                } else {
                    FillState::Pending
                }
            }
        }
    }
}

// ---------------------------------------------------------------- circuit breaker

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BreakerState {
    Closed,
    Open,
    HalfOpen,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BreakerConfig {
    /// This many failures in a row open the breaker.
    pub failure_threshold: u32,
    /// How long it stays open before one probe is let through.
    pub cooldown_ms: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct CircuitBreaker {
    cfg: BreakerConfig,
    state: BreakerState,
    failures: u32,
    opened_at: u64,
    probe_in_flight: bool,
}

impl CircuitBreaker {
    pub fn new(cfg: BreakerConfig) -> Self {
        CircuitBreaker {
            cfg,
            state: BreakerState::Closed,
            failures: 0,
            opened_at: 0,
            probe_in_flight: false,
        }
    }

    pub fn state(&self) -> BreakerState {
        self.state
    }

    pub fn failures(&self) -> u32 {
        self.failures
    }

    /// May a call go out now? Closed: yes. Open: no, until the cooldown has passed, then the breaker goes
    /// half open and lets exactly one probe through. Half open: no while the probe is out.
    pub fn allow(&mut self, now_ms: u64) -> bool {
        match self.state {
            BreakerState::Closed => true,
            BreakerState::Open => {
                if now_ms.saturating_sub(self.opened_at) >= self.cfg.cooldown_ms {
                    self.state = BreakerState::HalfOpen;
                    self.probe_in_flight = true;
                    true
                } else {
                    false
                }
            }
            BreakerState::HalfOpen => {
                if self.probe_in_flight {
                    false
                } else {
                    self.probe_in_flight = true;
                    true
                }
            }
        }
    }

    pub fn record_success(&mut self, _now_ms: u64) {
        match self.state {
            BreakerState::Closed => self.failures = 0,
            BreakerState::HalfOpen => {
                self.state = BreakerState::Closed;
                self.failures = 0;
                self.probe_in_flight = false;
            }
            // A late result from a call that went out before the breaker opened does not close it.
            BreakerState::Open => {}
        }
    }

    pub fn record_failure(&mut self, now_ms: u64) {
        match self.state {
            BreakerState::Closed => {
                self.failures = self.failures.saturating_add(1);
                if self.failures >= self.cfg.failure_threshold.max(1) {
                    self.state = BreakerState::Open;
                    self.opened_at = now_ms;
                }
            }
            BreakerState::HalfOpen => {
                self.state = BreakerState::Open;
                self.opened_at = now_ms;
                self.probe_in_flight = false;
            }
            BreakerState::Open => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(s: &str) -> Fixed {
        Fixed::parse(s).unwrap()
    }

    fn ladder() -> SlippageLadder {
        SlippageLadder {
            base_bps: 50,
            min_bps: 10,
            max_bps: 200,
            miss_step_bps: 25,
            fill_step_bps: 5,
            max_steps: 4,
            thin_step_bps: 40,
            deep_step_bps: 20,
        }
    }

    fn input(depth: &str, wanted: &str, misses: u32, fills: u32) -> LadderInput {
        LadderInput {
            depth: f(depth),
            wanted: f(wanted),
            recent_misses: misses,
            recent_fills: fills,
        }
    }

    #[test]
    fn a_middling_book_and_no_history_stays_at_the_base() {
        assert_eq!(ladder().allowed_bps(&input("30", "10", 0, 0)), 50);
    }

    #[test]
    fn a_thin_book_widens_by_the_thin_step() {
        assert_eq!(ladder().allowed_bps(&input("19", "10", 0, 0)), 90);
        assert_eq!(ladder().allowed_bps(&input("20", "10", 0, 0)), 50);
    }

    #[test]
    fn a_deep_book_tightens_by_the_deep_step() {
        assert_eq!(ladder().allowed_bps(&input("40", "10", 0, 0)), 30);
        assert_eq!(ladder().allowed_bps(&input("39.99", "10", 0, 0)), 50);
    }

    #[test]
    fn each_miss_widens_and_each_fill_tightens_up_to_max_steps() {
        let l = ladder();
        assert_eq!(l.allowed_bps(&input("30", "10", 2, 0)), 100);
        assert_eq!(l.allowed_bps(&input("30", "10", 4, 0)), 150);
        assert_eq!(
            l.allowed_bps(&input("30", "10", 40, 0)),
            150,
            "misses beyond max_steps do not count"
        );
        assert_eq!(l.allowed_bps(&input("30", "10", 0, 3)), 35);
        assert_eq!(l.allowed_bps(&input("30", "10", 0, 40)), 30);
    }

    #[test]
    fn the_result_stays_inside_the_bounds() {
        let mut l = ladder();
        l.max_bps = 120;
        l.min_bps = 15;
        assert_eq!(l.allowed_bps(&input("1", "10", 4, 0)), 120);
        assert_eq!(l.allowed_bps(&input("100", "10", 0, 4)), 15);
    }

    #[test]
    fn swapped_bounds_are_read_as_a_range() {
        let mut l = ladder();
        l.min_bps = 200;
        l.max_bps = 10;
        let v = l.allowed_bps(&input("30", "10", 0, 0));
        assert!((10..=200).contains(&v));
    }

    #[test]
    fn a_buy_refuses_a_price_that_rose_past_the_threshold() {
        assert!(momentum_check(OrderSide::Buy, f("0.50"), f("0.5025"), 50).is_ok());
        let r = momentum_check(OrderSide::Buy, f("0.50"), f("0.5026"), 50).unwrap_err();
        assert_eq!(r.moved_bps, 52);
        assert_eq!(r.max_bps, 50);
    }

    #[test]
    fn a_sell_refuses_a_price_that_fell_past_the_threshold() {
        assert!(momentum_check(OrderSide::Sell, f("0.50"), f("0.4975"), 50).is_ok());
        assert!(momentum_check(OrderSide::Sell, f("0.50"), f("0.4974"), 50).is_err());
    }

    #[test]
    fn a_move_in_the_followers_favour_is_never_refused() {
        assert!(momentum_check(OrderSide::Buy, f("0.50"), f("0.40"), 0).is_ok());
        assert!(momentum_check(OrderSide::Sell, f("0.50"), f("0.60"), 0).is_ok());
    }

    #[test]
    fn the_move_is_rounded_up_so_a_borderline_case_is_refused() {
        // 50.01 bps of movement reads as 51 and is refused at a threshold of 50.
        assert!(momentum_check(OrderSide::Buy, f("1.0"), f("1.005001"), 50).is_err());
    }

    #[test]
    fn a_fill_counts_only_after_the_window() {
        let mut s = FillStabilizer::new(100);
        assert_eq!(s.observe(0, f("4")), FillState::Pending);
        assert_eq!(s.observe(99, f("4")), FillState::Pending);
        assert_eq!(s.observe(100, f("4")), FillState::Stable(f("4")));
    }

    #[test]
    fn a_change_restarts_the_window() {
        let mut s = FillStabilizer::new(100);
        s.observe(0, f("4"));
        assert_eq!(s.observe(90, f("5")), FillState::Pending);
        assert_eq!(s.observe(189, f("5")), FillState::Pending);
        assert_eq!(s.observe(190, f("5")), FillState::Stable(f("5")));
    }

    #[test]
    fn a_stable_fill_that_changes_again_is_pending_again() {
        let mut s = FillStabilizer::new(10);
        s.observe(0, f("1"));
        assert_eq!(s.observe(10, f("1")), FillState::Stable(f("1")));
        assert_eq!(s.observe(11, f("2")), FillState::Pending);
    }

    #[test]
    fn a_zero_window_counts_the_first_reading() {
        let mut s = FillStabilizer::new(0);
        assert_eq!(s.observe(5, f("3")), FillState::Stable(f("3")));
    }

    fn breaker() -> CircuitBreaker {
        CircuitBreaker::new(BreakerConfig {
            failure_threshold: 3,
            cooldown_ms: 1000,
        })
    }

    #[test]
    fn it_opens_after_the_threshold_of_failures_in_a_row() {
        let mut b = breaker();
        b.record_failure(0);
        b.record_failure(1);
        assert_eq!(b.state(), BreakerState::Closed);
        b.record_failure(2);
        assert_eq!(b.state(), BreakerState::Open);
        assert!(!b.allow(3));
    }

    #[test]
    fn a_success_resets_the_failure_count() {
        let mut b = breaker();
        b.record_failure(0);
        b.record_failure(1);
        b.record_success(2);
        assert_eq!(b.failures(), 0);
        b.record_failure(3);
        b.record_failure(4);
        assert_eq!(b.state(), BreakerState::Closed);
    }

    #[test]
    fn after_the_cooldown_one_probe_goes_through_and_a_second_waits() {
        let mut b = breaker();
        for t in 0..3 {
            b.record_failure(t);
        }
        assert!(!b.allow(1001), "opened at 2, cooldown ends at 1002");
        assert!(b.allow(1002));
        assert_eq!(b.state(), BreakerState::HalfOpen);
        assert!(!b.allow(1003), "the probe is still out");
    }

    #[test]
    fn a_successful_probe_closes_it_and_a_failed_one_reopens_it_with_a_fresh_cooldown() {
        let mut b = breaker();
        for t in 0..3 {
            b.record_failure(t);
        }
        assert!(b.allow(2000));
        b.record_success(2001);
        assert_eq!(b.state(), BreakerState::Closed);
        assert!(b.allow(2002));

        let mut b = breaker();
        for t in 0..3 {
            b.record_failure(t);
        }
        assert!(b.allow(2000));
        b.record_failure(2001);
        assert_eq!(b.state(), BreakerState::Open);
        assert!(!b.allow(2500), "the cooldown started again at 2001");
        assert!(b.allow(3001));
    }

    #[test]
    fn a_late_success_while_open_does_not_close_it() {
        let mut b = breaker();
        for t in 0..3 {
            b.record_failure(t);
        }
        b.record_success(10);
        assert_eq!(b.state(), BreakerState::Open);
    }

    #[test]
    fn a_threshold_of_zero_is_read_as_one() {
        let mut b = CircuitBreaker::new(BreakerConfig {
            failure_threshold: 0,
            cooldown_ms: 5,
        });
        b.record_failure(0);
        assert_eq!(b.state(), BreakerState::Open);
    }
}
