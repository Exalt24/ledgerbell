//! The wire messages: JSON text frames shaped like Binance spot's depth stream and its WebSocket API
//! `depth` request (written from Binance's public documentation, see the README).
//!
//! * Stream event: `{"e":"depthUpdate","E":..,"s":"BTCUSDT","U":first,"u":final,"b":[[price,qty]],"a":[[price,qty]]}`
//! * Snapshot request: `{"id":"..","method":"depth","params":{"symbol":"BTCUSDT","limit":1000}}`
//! * Snapshot reply: `{"id":"..","status":200,"result":{"lastUpdateId":..,"bids":[[price,qty]],"asks":[[price,qty]]}}`

use serde_json::{json, Value};
use std::fmt;

use crate::book::{Delta, Fixed, Level};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotReply {
    pub id: String,
    pub last_update_id: u64,
    pub bids: Vec<Level>,
    pub asks: Vec<Level>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Inbound {
    Depth(Delta),
    Snapshot(SnapshotReply),
    /// A reply to one of our requests with a status other than 200.
    Failure {
        id: String,
        status: i64,
        message: String,
    },
    /// Valid JSON that is none of the above: ignored.
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtoError(pub String);

impl fmt::Display for ProtoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "protocol error: {}", self.0)
    }
}

impl std::error::Error for ProtoError {}

fn err<T>(msg: impl Into<String>) -> Result<T, ProtoError> {
    Err(ProtoError(msg.into()))
}

fn levels(v: &Value, what: &str) -> Result<Vec<Level>, ProtoError> {
    let arr = v
        .as_array()
        .ok_or_else(|| ProtoError(format!("{what} is not an array")))?;
    let mut out = Vec::with_capacity(arr.len());
    for pair in arr {
        let p = pair
            .as_array()
            .filter(|p| p.len() >= 2)
            .ok_or_else(|| ProtoError(format!("{what}: bad level")))?;
        let price = p[0]
            .as_str()
            .ok_or_else(|| ProtoError(format!("{what}: price is not a string")))?;
        let qty = p[1]
            .as_str()
            .ok_or_else(|| ProtoError(format!("{what}: size is not a string")))?;
        out.push(Level {
            price: Fixed::parse(price).map_err(|e| ProtoError(e.to_string()))?,
            qty: Fixed::parse(qty).map_err(|e| ProtoError(e.to_string()))?,
        });
    }
    Ok(out)
}

pub fn parse(text: &str) -> Result<Inbound, ProtoError> {
    let v: Value = serde_json::from_str(text).map_err(|e| ProtoError(format!("bad json: {e}")))?;
    if v.get("e").and_then(Value::as_str) == Some("depthUpdate") {
        let first = v
            .get("U")
            .and_then(Value::as_u64)
            .ok_or_else(|| ProtoError("depthUpdate without U".into()))?;
        let last = v
            .get("u")
            .and_then(Value::as_u64)
            .ok_or_else(|| ProtoError("depthUpdate without u".into()))?;
        let bids = levels(v.get("b").unwrap_or(&Value::Null), "b")?;
        let asks = levels(v.get("a").unwrap_or(&Value::Null), "a")?;
        return Ok(Inbound::Depth(Delta {
            first_id: first,
            final_id: last,
            bids,
            asks,
        }));
    }
    if let Some(status) = v.get("status").and_then(Value::as_i64) {
        let id = match v.get("id") {
            Some(Value::String(s)) => s.clone(),
            Some(other) => other.to_string(),
            None => return err("reply without id"),
        };
        if status != 200 {
            let message = v
                .pointer("/error/msg")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            return Ok(Inbound::Failure { id, status, message });
        }
        let r = v
            .get("result")
            .ok_or_else(|| ProtoError("reply without result".into()))?;
        let last_update_id = r
            .get("lastUpdateId")
            .and_then(Value::as_u64)
            .ok_or_else(|| ProtoError("reply without lastUpdateId".into()))?;
        let bids = levels(r.get("bids").unwrap_or(&Value::Null), "bids")?;
        let asks = levels(r.get("asks").unwrap_or(&Value::Null), "asks")?;
        return Ok(Inbound::Snapshot(SnapshotReply {
            id,
            last_update_id,
            bids,
            asks,
        }));
    }
    Ok(Inbound::Other)
}

pub fn depth_request(id: &str, symbol: &str, limit: u32) -> String {
    json!({"id": id, "method": "depth", "params": {"symbol": symbol, "limit": limit}}).to_string()
}

fn level_json(l: &Level) -> Value {
    json!([l.price.to_string(), l.qty.to_string()])
}

pub fn depth_update_json(symbol: &str, event_time_ms: u64, d: &Delta) -> String {
    json!({
        "e": "depthUpdate",
        "E": event_time_ms,
        "s": symbol,
        "U": d.first_id,
        "u": d.final_id,
        "b": d.bids.iter().map(level_json).collect::<Vec<_>>(),
        "a": d.asks.iter().map(level_json).collect::<Vec<_>>(),
    })
    .to_string()
}

pub fn snapshot_reply_json(id: &str, last_update_id: u64, bids: &[Level], asks: &[Level]) -> String {
    json!({
        "id": id,
        "status": 200,
        "result": {
            "lastUpdateId": last_update_id,
            "bids": bids.iter().map(level_json).collect::<Vec<_>>(),
            "asks": asks.iter().map(level_json).collect::<Vec<_>>(),
        }
    })
    .to_string()
}

pub fn failure_reply_json(id: &str, status: i64, msg: &str) -> String {
    json!({"id": id, "status": status, "error": {"code": -1, "msg": msg}}).to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_depth_update_round_trips() {
        let d = Delta {
            first_id: 157,
            final_id: 160,
            bids: vec![Level {
                price: Fixed::parse("0.0024").unwrap(),
                qty: Fixed::parse("10").unwrap(),
            }],
            asks: vec![Level {
                price: Fixed::parse("0.0026").unwrap(),
                qty: Fixed::ZERO,
            }],
        };
        let text = depth_update_json("BNBBTC", 1_672_515_782_136, &d);
        assert_eq!(parse(&text).unwrap(), Inbound::Depth(d));
    }

    #[test]
    fn the_documented_example_event_parses() {
        let text = r#"{"e":"depthUpdate","E":1672515782136,"s":"BNBBTC","U":157,"u":160,"b":[["0.0024","10"]],"a":[["0.0026","100"]]}"#;
        let Inbound::Depth(d) = parse(text).unwrap() else {
            panic!("not a depth update")
        };
        assert_eq!((d.first_id, d.final_id), (157, 160));
        assert_eq!(d.asks[0].qty, Fixed::parse("100").unwrap());
    }

    #[test]
    fn a_snapshot_reply_parses_with_its_id() {
        let text = snapshot_reply_json("req-1", 99, &[], &[]);
        assert_eq!(
            parse(&text).unwrap(),
            Inbound::Snapshot(SnapshotReply {
                id: "req-1".into(),
                last_update_id: 99,
                bids: vec![],
                asks: vec![]
            })
        );
    }

    #[test]
    fn a_failure_reply_keeps_its_status_and_message() {
        let text = failure_reply_json("r", 429, "slow down");
        assert_eq!(
            parse(&text).unwrap(),
            Inbound::Failure {
                id: "r".into(),
                status: 429,
                message: "slow down".into()
            }
        );
    }

    #[test]
    fn a_price_or_size_that_is_a_json_number_is_refused() {
        assert!(parse(r#"{"e":"depthUpdate","U":1,"u":2,"b":[[1,"1"]],"a":[]}"#).is_err());
        assert!(parse(r#"{"e":"depthUpdate","U":1,"u":2,"b":[["1",1]],"a":[]}"#).is_err());
        assert!(parse(r#"{"e":"depthUpdate","U":1,"u":2,"b":[["1"]],"a":[]}"#).is_err());
    }

    #[test]
    fn a_protocol_error_prints_its_message() {
        assert_eq!(ProtoError("bad thing".into()).to_string(), "protocol error: bad thing");
    }

    #[test]
    fn junk_is_an_error_and_unknown_json_is_ignored() {
        assert!(parse("not json").is_err());
        assert!(parse(r#"{"e":"depthUpdate","U":1}"#).is_err());
        assert!(parse(r#"{"e":"depthUpdate","U":1,"u":2,"b":[["x","1"]],"a":[]}"#).is_err());
        assert_eq!(parse(r#"{"result":null,"id":1}"#).unwrap(), Inbound::Other);
    }
}
