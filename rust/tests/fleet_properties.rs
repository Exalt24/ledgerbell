//! Property tests for the fleet pieces: sequence ids, the execution guards, the failover state machine,
//! EIP-712 signing and the order amounts. Each states an invariant and lets proptest look for a counterexample.

use std::time::Duration;

use bookwatch::book::Fixed;
use bookwatch::clob::{amounts, Order};
use bookwatch::eip712::{recover_address, Value, Wallet};
use bookwatch::follow::OrderSide;
use bookwatch::guard::{
    momentum_check, BreakerConfig, BreakerState, CircuitBreaker, FillStabilizer, FillState, LadderInput, SlippageLadder,
};
use bookwatch::seq::{classify, SeqVerdict};
use bookwatch::worker::Failover;
use proptest::prelude::*;

fn ladder_strategy() -> impl Strategy<Value = SlippageLadder> {
    (
        0u32..500,
        0u32..500,
        0u32..500,
        0u32..100,
        0u32..100,
        0u32..8,
        0u32..200,
        0u32..200,
    )
        .prop_map(|(base, a, b, miss, fill, steps, thin, deep)| SlippageLadder {
            base_bps: base,
            min_bps: a.min(b),
            max_bps: a.max(b),
            miss_step_bps: miss,
            fill_step_bps: fill,
            max_steps: steps,
            thin_step_bps: thin,
            deep_step_bps: deep,
        })
}

fn input(depth: u64, wanted: u64, misses: u32, fills: u32) -> LadderInput {
    LadderInput {
        depth: Fixed(depth),
        wanted: Fixed(wanted),
        recent_misses: misses,
        recent_fills: fills,
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(512))]

    // ---- sequence ids

    #[test]
    fn exactly_one_verdict_and_it_matches_the_arithmetic(last in 0u64..1_000_000, incoming in 0u64..1_000_000) {
        let v = classify(last, incoming);
        if incoming == 0 {
            prop_assert_eq!(v, SeqVerdict::Invalid);
        } else if incoming == last + 1 {
            prop_assert_eq!(v, SeqVerdict::Apply);
        } else if incoming <= last {
            prop_assert_eq!(v, SeqVerdict::Duplicate);
        } else {
            prop_assert_eq!(v, SeqVerdict::Gap { expected: last + 1 });
        }
    }

    /// A receiver that applies only the next id, drops repeats and asks for a replay at a hole ends with every
    /// id applied exactly once and in order, whatever holes and repeats the sender produced.
    #[test]
    fn a_receiver_ends_with_every_id_once_in_order(n in 1u64..60, noise in proptest::collection::vec(0u8..4, 0..120)) {
        // The first send: 1..=n, with some ids left out, some repeated, some moved later.
        let mut first: Vec<u64> = Vec::new();
        let mut late: Vec<u64> = Vec::new();
        for (i, id) in (1..=n).enumerate() {
            match noise.get(i).copied().unwrap_or(0) {
                0 | 1 => first.push(id),
                2 => {}
                _ => { first.push(id); first.push(id); }
            }
            if noise.get(i).copied() == Some(1) && id > 1 {
                late.push(id - 1);
            }
        }
        first.extend(late);
        let mut acked = 0u64;
        let mut applied: Vec<u64> = Vec::new();
        let mut queue: std::collections::VecDeque<u64> = first.into();
        let mut replays = 0;
        let mut asked_after: Option<u64> = None;
        while let Some(id) = queue.pop_front() {
            match classify(acked, id) {
                SeqVerdict::Apply => { acked = id; applied.push(id); asked_after = None; }
                SeqVerdict::Duplicate | SeqVerdict::Invalid => {}
                SeqVerdict::Gap { .. } => {
                    if asked_after != Some(acked) {
                        asked_after = Some(acked);
                        replays += 1;
                        // The replay is sent after what is already in flight.
                        queue.extend(acked + 1..=n);
                    }
                }
            }
        }
        // Ids the first send never reached and that were never followed by a later one are the stream's tail:
        // a receiver cannot see a hole at the very end, so a replay is asked for at the next connect.
        if acked < n {
            queue.extend(acked + 1..=n);
            while let Some(id) = queue.pop_front() {
                if classify(acked, id) == SeqVerdict::Apply { acked = id; applied.push(id); }
            }
        }
        prop_assert_eq!(applied, (1..=n).collect::<Vec<_>>());
        prop_assert!(replays <= n as usize + 1);
    }

    // ---- slippage ladder

    #[test]
    fn the_ladder_never_leaves_its_bounds(l in ladder_strategy(), depth in 0u64..1_000_000_000_000, wanted in 0u64..1_000_000_000_000, m in 0u32..20, f in 0u32..20) {
        let v = l.allowed_bps(&input(depth, wanted, m, f));
        prop_assert!(v >= l.min_bps && v <= l.max_bps);
    }

    #[test]
    fn more_misses_never_tighten_and_more_fills_never_widen(l in ladder_strategy(), depth in 0u64..1_000_000_000, wanted in 1u64..1_000_000_000, m in 0u32..20, f in 0u32..20) {
        let base = l.allowed_bps(&input(depth, wanted, m, f));
        prop_assert!(l.allowed_bps(&input(depth, wanted, m + 1, f)) >= base);
        prop_assert!(l.allowed_bps(&input(depth, wanted, m, f + 1)) <= base);
    }

    #[test]
    fn a_deeper_book_never_widens_the_cap(l in ladder_strategy(), depth in 0u64..1_000_000_000, extra in 0u64..1_000_000_000, wanted in 1u64..1_000_000_000, m in 0u32..20, f in 0u32..20) {
        let shallow = l.allowed_bps(&input(depth, wanted, m, f));
        let deep = l.allowed_bps(&input(depth + extra, wanted, m, f));
        prop_assert!(deep <= shallow);
    }

    // ---- momentum filter

    #[test]
    fn a_move_in_the_followers_favour_is_never_refused(leader in 1u64..10_000_000_000, delta in 0u64..10_000_000_000, max in 0u32..10_000) {
        let cheaper = Fixed(leader.saturating_sub(delta));
        prop_assert!(momentum_check(OrderSide::Buy, Fixed(leader), cheaper, max).is_ok());
        let dearer = Fixed(leader.saturating_add(delta));
        prop_assert!(momentum_check(OrderSide::Sell, Fixed(leader), dearer, max).is_ok());
    }

    #[test]
    fn a_refusal_means_the_move_exceeded_the_threshold_and_a_looser_threshold_accepts_more(leader in 1u64..10_000_000_000, current in 0u64..20_000_000_000, max in 0u32..5_000, looser in 0u32..5_000) {
        for side in [OrderSide::Buy, OrderSide::Sell] {
            if let Err(r) = momentum_check(side, Fixed(leader), Fixed(current), max) {
                prop_assert!(r.moved_bps > max);
                prop_assert_eq!(r.max_bps, max);
                if looser <= max {
                    prop_assert!(momentum_check(side, Fixed(leader), Fixed(current), looser).is_err());
                }
            } else if looser >= max {
                prop_assert!(momentum_check(side, Fixed(leader), Fixed(current), looser).is_ok());
            }
        }
    }

    // ---- fill stabilization

    /// Checked against a model that works from the readings alone: a fill is stable at `t` exactly when the
    /// value has been the same since a reading at least `window` earlier.
    #[test]
    fn a_fill_is_stable_exactly_when_it_has_not_changed_for_the_window(window in 0u64..50, steps in proptest::collection::vec((0u64..20, 0u64..4), 1..40)) {
        let mut s = FillStabilizer::new(window);
        let mut t = 0u64;
        let mut readings: Vec<(u64, u64)> = Vec::new();
        for (dt, v) in steps {
            t += dt;
            readings.push((t, v));
            let got = s.observe(t, Fixed(v));
            // Model: the start of the trailing run of equal values.
            let run_start = readings.iter().rev().take_while(|(_, x)| *x == v).last().unwrap().0;
            let want = if t - run_start >= window { FillState::Stable(Fixed(v)) } else { FillState::Pending };
            prop_assert_eq!(got, want);
        }
    }

    // ---- circuit breaker

    #[test]
    fn the_breaker_keeps_its_rules_under_any_sequence_of_calls(threshold in 1u32..6, cooldown in 1u64..200, ops in proptest::collection::vec((0u8..3, 0u64..60), 1..120)) {
        let mut b = CircuitBreaker::new(BreakerConfig { failure_threshold: threshold, cooldown_ms: cooldown });
        let mut now = 0u64;
        let mut opened_at: Option<u64> = None;
        let mut probes_out = 0u32;
        for (op, dt) in ops {
            now += dt;
            let before = b.state();
            match op {
                0 => {
                    let allowed = b.allow(now);
                    if before == BreakerState::Closed {
                        prop_assert!(allowed);
                    }
                    if before == BreakerState::Open {
                        let early = now - opened_at.unwrap() < cooldown;
                        prop_assert_eq!(allowed, !early, "an open breaker lets one call through exactly when the cooldown is over");
                        if allowed { probes_out += 1; }
                    }
                    if before == BreakerState::HalfOpen {
                        prop_assert!(!allowed || probes_out == 0);
                        if allowed { probes_out += 1; }
                    }
                }
                1 => { b.record_success(now); if before != BreakerState::Open { probes_out = 0; } }
                _ => { b.record_failure(now); if before != BreakerState::Open { probes_out = 0; } }
            }
            let after = b.state();
            if before != BreakerState::Open && after == BreakerState::Open {
                opened_at = Some(now);
            }
            if after == BreakerState::Closed {
                prop_assert!(b.failures() < threshold);
            }
            if after == BreakerState::Open {
                prop_assert!(b.failures() >= threshold);
            }
            prop_assert!(probes_out <= 1, "never two probes out at once");
        }
    }

    // ---- reconnect and failover

    #[test]
    fn failover_stays_in_range_and_waits_inside_its_limits(n in 1usize..5, threshold in 1u32..5, base in 1u64..200, extra in 0u64..2000, events in proptest::collection::vec(proptest::bool::ANY, 1..80)) {
        let cap = base + extra;
        let mut fo = Failover::new(n, threshold, Duration::from_millis(base), Duration::from_millis(cap));
        let mut on_current = 0u32;
        let mut expect_current = 0usize;
        for connected in events {
            if connected {
                fo.on_connected();
                on_current = 0;
            } else {
                let (wait, switched) = fo.on_failure();
                on_current += 1;
                let should_switch = n > 1 && on_current >= threshold;
                prop_assert_eq!(switched, should_switch);
                if should_switch {
                    expect_current = (expect_current + 1) % n;
                    on_current = 0;
                }
                prop_assert!(wait >= Duration::from_millis(base) && wait <= Duration::from_millis(cap));
            }
            prop_assert!(fo.current() < n);
            prop_assert_eq!(fo.current(), expect_current);
        }
    }

    // ---- EIP-712 and orders

    #[test]
    fn a_signature_recovers_to_the_signing_address_and_to_no_other(secret in proptest::array::uniform32(any::<u8>()), digest in proptest::array::uniform32(any::<u8>())) {
        let Ok(w) = Wallet::from_secret(&secret) else { return Ok(()); };
        let sig = w.sign_digest(&digest).unwrap();
        prop_assert_eq!(recover_address(&digest, &sig).unwrap(), w.address());
        let mut other = digest;
        other[0] ^= 1;
        prop_assert_ne!(recover_address(&other, &sig).unwrap_or([0; 20]), w.address());
    }

    #[test]
    fn signing_is_deterministic(secret in proptest::array::uniform32(any::<u8>()), digest in proptest::array::uniform32(any::<u8>())) {
        let Ok(w) = Wallet::from_secret(&secret) else { return Ok(()); };
        prop_assert_eq!(w.sign_digest(&digest).unwrap(), w.sign_digest(&digest).unwrap());
    }

    #[test]
    fn decimal_integers_parse_to_the_same_word_as_the_number(n in any::<u128>()) {
        prop_assert_eq!(Value::uint_decimal(&n.to_string()).unwrap(), Value::uint(n));
    }

    #[test]
    fn the_amounts_never_imply_a_price_worse_than_the_limit(qty in 1_000_000u64..100_000_000_000, price in 1u64..99_999_999, buy in proptest::bool::ANY) {
        let side = if buy { OrderSide::Buy } else { OrderSide::Sell };
        let Ok((maker, taker)) = amounts(side, Fixed(qty), Fixed(price)) else { return Ok(()); };
        let (w, _) = Wallet::throwaway();
        let a = w.address();
        let o = Order {
            salt: 1, maker: a, signer: a, token_id: "1".into(), maker_amount: maker, taker_amount: taker,
            side, signature_type: 0, timestamp_ms: 0, metadata: [0; 32], builder: [0; 32], expiration: 0,
        };
        prop_assert!(o.shares().0 <= qty, "never more shares than asked for");
        match side {
            OrderSide::Buy => prop_assert!(o.implied_price().0 <= price + 1, "a buy pays no more than the limit (one unit of rounding in the readback)"),
            OrderSide::Sell => prop_assert!(o.implied_price().0 + 1 >= price, "a sell takes no less than the limit"),
        }
    }

    #[test]
    fn two_orders_that_differ_in_the_salt_or_the_amount_have_different_digests(salt in 0u64..1_000_000, other in 0u64..1_000_000, amount in 1u64..1_000_000_000) {
        prop_assume!(salt != other);
        let (w, _) = Wallet::throwaway();
        let a = w.address();
        let make = |salt: u64, amount: u64| Order {
            salt, maker: a, signer: a, token_id: "1001".into(), maker_amount: amount, taker_amount: 1_000_000,
            side: OrderSide::Buy, signature_type: 0, timestamp_ms: 7, metadata: [0; 32], builder: [0; 32], expiration: 0,
        };
        prop_assert_ne!(make(salt, amount).digest().unwrap(), make(other, amount).digest().unwrap());
        prop_assert_ne!(make(salt, amount).digest().unwrap(), make(salt, amount + 1).digest().unwrap());
    }
}
