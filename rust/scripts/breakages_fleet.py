# LIFESPAN: permanent
"""Deliberate breakages for the fleet code (sequence ids, guards, EIP-712, SQLite store, the CLOB stand-in, the
C2 and the worker). Each entry is (file in src/, rule, exact source text that holds it, text that breaks it);
`breakages.py` appends these to its own list and runs the whole suite after each one."""

FLEET_BREAKAGES = [
    # -- sequence ids
    ("seq.rs", "a skipped id is a gap", "std::cmp::Ordering::Greater => SeqVerdict::Gap { expected },", "std::cmp::Ordering::Greater => SeqVerdict::Duplicate,"),
    ("seq.rs", "id zero is invalid", "if incoming == 0 {\n        return SeqVerdict::Invalid;", "if false {\n        return SeqVerdict::Invalid;"),
    # -- the slippage ladder
    ("guard.rs", "a miss widens the cap", "bps += misses * self.miss_step_bps as i64;", "bps -= misses * self.miss_step_bps as i64;"),
    ("guard.rs", "a clean fill tightens the cap", "bps -= fills * self.fill_step_bps as i64;", "bps += fills * self.fill_step_bps as i64;"),
    ("guard.rs", "a thin book widens the cap", "if depth < wanted * 2 {", "if depth > wanted * 2 {"),
    ("guard.rs", "a deep book tightens the cap", "} else if depth >= wanted * 4 {", "} else if depth < wanted * 4 {"),
    ("guard.rs", "the cap stays inside its bounds", "bps.clamp(lo, hi) as u32", "bps.max(0) as u32"),
    # -- the momentum filter
    ("guard.rs", "a buy is refused when the price rose", "OrderSide::Buy => current.0.saturating_sub(leader_price.0),", "OrderSide::Buy => leader_price.0.saturating_sub(current.0),"),
    ("guard.rs", "the move is rounded up", "let moved = (adverse * 10_000).div_ceil(leader_price.0 as u128);", "let moved = (adverse * 10_000) / (leader_price.0 as u128);"),
    ("guard.rs", "a move past the threshold is refused", "if moved > max_adverse_bps {", "if moved >= max_adverse_bps {"),
    # -- fill stabilization
    ("guard.rs", "a fill counts only after the whole window", "if now_ms.saturating_sub(since) >= self.window_ms {", "if now_ms.saturating_sub(since) > self.window_ms {"),
    ("guard.rs", "a change restarts the window", "self.current = Some((matched, now_ms));", "self.current = Some((matched, self.current.map_or(now_ms, |c| c.1)));"),
    # -- the circuit breaker
    ("guard.rs", "the failure threshold opens the breaker", "if self.failures >= self.cfg.failure_threshold.max(1) {", "if self.failures > self.cfg.failure_threshold.max(1) {"),
    ("guard.rs", "the cooldown ends exactly at its length", "if now_ms.saturating_sub(self.opened_at) >= self.cfg.cooldown_ms {", "if now_ms.saturating_sub(self.opened_at) > self.cfg.cooldown_ms {"),
    ("guard.rs", "a second call waits while the probe is out", "if self.probe_in_flight {", "if false {"),
    ("guard.rs", "a good probe closes the breaker", "self.state = BreakerState::Closed;", "self.state = BreakerState::HalfOpen;"),
    ("guard.rs", "a failed probe restarts the cooldown", "BreakerState::HalfOpen => {\n                self.state = BreakerState::Open;\n                self.opened_at = now_ms;", "BreakerState::HalfOpen => {\n                self.state = BreakerState::Open;"),
    ("guard.rs", "a success resets the failure count", "BreakerState::Closed => self.failures = 0,", "BreakerState::Closed => {}"),
    # -- EIP-712
    ("eip712.rs", "the digest starts with 0x19 0x01", "buf.extend_from_slice(&[0x19, 0x01]);", "buf.extend_from_slice(&[0x19, 0x00]);"),
    ("eip712.rs", "dependent types are sorted", "rest.sort();", "let _ = &rest;"),
    ("eip712.rs", "v is 27 or 28", "out[64] = 27 + rid.to_byte();", "out[64] = rid.to_byte();"),
    ("eip712.rs", "an address is left padded", "w[12..].copy_from_slice(a);", "w[..20].copy_from_slice(a);"),
    ("eip712.rs", "a uint must fit its width", "if w[..spare].iter().any(|b| *b != 0) {", "if false {"),
    ("eip712.rs", "fixed bytes keep their length", "if b.len() != n {", "if false {"),
    ("eip712.rs", "the address is the last 20 bytes of the key hash", "let h = keccak256(&point.as_bytes()[1..]);", "let h = keccak256(&point.as_bytes()[..]);"),
    # -- the SQLite store
    ("store.rs", "the database is put in WAL mode", '"PRAGMA journal_mode=WAL"', '"PRAGMA journal_mode=DELETE"'),
    ("store.rs", "full sync is honoured", 'if full_sync { "FULL" } else { "NORMAL" }', '"NORMAL"'),
    ("store.rs", "a busy database is waited for", "conn.busy_timeout(Duration::from_secs(10))?;", "conn.busy_timeout(Duration::ZERO)?;"),
    ("store.rs", "only the next command can finish", "if seq != last + 1 {", "if false {"),
    ("store.rs", "an order that exists is resumed, not rebuilt", "if let Some(o) = existing {\n            return Ok((o, true));", "if let Some(o) = existing {\n            let _ = o;"),
    ("store.rs", "a nonce is one more than the last", "DO UPDATE SET next = next + 1 RETURNING next - 1", "DO UPDATE SET next = next + 2 RETURNING next - 2"),
    ("store.rs", "finish moves last_acked forward", "ON CONFLICT(k) DO UPDATE SET v = excluded.v", "ON CONFLICT(k) DO NOTHING"),
    ("store.rs", "an order and its nonce are taken under the write lock", "let tx = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;\n        let existing = tx", "let tx = self.conn.transaction_with_behavior(TransactionBehavior::Deferred)?;\n        let existing = tx"),
    # -- the CLOB order and its stand-in
    ("clob.rs", "a buy's collateral rounds down", "OrderSide::Buy => notional / scale,", "OrderSide::Buy => notional.div_ceil(scale),"),
    ("clob.rs", "a sell's collateral rounds up", "OrderSide::Sell => notional.div_ceil(scale),", "OrderSide::Sell => notional / scale,"),
    ("clob.rs", "shares are in 6 decimals", "let shares = qty.0 / 100;", "let shares = qty.0 / 10;"),
    ("clob.rs", "the CLOB checks who signed", "Ok(a) if a == order.signer => {}", "Ok(_) => {}"),
    ("clob.rs", "an EOA order must be its own maker", "if order.signature_type == 0 && order.maker != order.signer {", "if false {"),
    ("clob.rs", "a salt is used once per maker", "if !st.salts.lock().unwrap().insert((maker_hex.clone(), order.salt)) {", "if false {"),
    # -- the C2 server
    ("c2.rs", "a hello must be numbered one", "if classify(0, env.seq) != SeqVerdict::Apply {", "if false {"),
    ("c2.rs", "the window holds commands back", "next > e.last_acked + st.cfg.window {", "next > e.last_acked + st.cfg.window + 1000 {"),
    ("c2.rs", "an ack moves the worker's last_acked", "e.last_acked = e.last_acked.max(a.acked_seq);", "e.last_acked = e.last_acked;"),
    ("c2.rs", "a replay resends from the id after the last stored one", "next = r.after_seq + 1;", "next = r.after_seq + 2;"),
    # -- the worker
    ("worker.rs", "a repeated id is dropped, a hole is replayed", "match classify(acked, env.seq) {", "match { let _ = classify(acked, env.seq); SeqVerdict::Apply } {"),
    ("worker.rs", "one replay is asked for one hole", "if replay_after != Some(acked) {", "if true {"),
    ("worker.rs", "an open breaker skips the command", "if !self.breaker.allow(now) {", "if false {"),
    ("worker.rs", "the momentum filter has the configured threshold", "momentum_check(side, leader_price, best.price, self.cfg.momentum_max_bps)", "momentum_check(side, leader_price, best.price, u32::MAX)"),
    ("worker.rs", "recent misses reach the ladder", "recent_misses: self.misses,", "recent_misses: 0,"),
    ("worker.rs", "recent fills reach the ladder", "recent_fills: self.fills,", "recent_fills: 0,"),
    ("worker.rs", "a stable but smaller fill is partial", "if counted && matched >= want && !want.is_zero() {", "if counted && !want.is_zero() {"),
    ("worker.rs", "failover switches at the threshold", "if self.n > 1 && self.on_current >= self.threshold {", "if self.n > 1 && self.on_current > self.threshold {"),
    ("worker.rs", "the reconnect wait doubles", "let shift = (self.on_current - 1).min(20);", "let shift = 0;"),
]
