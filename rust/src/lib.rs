//! bookwatch: an async order book client for a depth stream, and a stand-in exchange to test it against.
//!
//! * [`book`]: the local book (integer prices, a delta rule that refuses gaps, stale and crossing deltas).
//! * [`client`]: the Tokio task that connects, syncs, resyncs on a gap and reconnects with backoff.
//! * [`follow`]: `delta_position`, a pure function from a leader's position change to a follower order.
//! * [`proto`]: the JSON frames.
//! * [`standin`]: the stand-in exchange.

pub mod book;
pub mod c2;
pub mod client;
pub mod clob;
pub mod eip712;
pub mod follow;
pub mod guard;
pub mod proto;
pub mod seq;
pub mod standin;
pub mod store;
pub mod wire;
pub mod worker;
