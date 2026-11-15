//! The Protobuf frames between a C2 and its workers. The messages come from `proto/fleet.proto`, compiled by
//! `build.rs` with protox (no `protoc` needed), and travel as binary WebSocket frames, one `Envelope` each.

use prost::Message;

#[allow(clippy::all)]
pub mod pb {
    include!(concat!(env!("OUT_DIR"), "/fleet.v1.rs"));
}

pub use pb::envelope::Body;
pub use pb::{Ack, Envelope, Heartbeat, Hello, Outcome, PositionDelta, ReplayRequest};

pub fn encode(seq: u64, body: Body) -> Vec<u8> {
    Envelope { seq, body: Some(body) }.encode_to_vec()
}

pub fn decode(bytes: &[u8]) -> Result<Envelope, prost::DecodeError> {
    Envelope::decode(bytes)
}

impl Outcome {
    pub fn label(self) -> &'static str {
        match self {
            Outcome::Unspecified => "unspecified",
            Outcome::Filled => "filled",
            Outcome::Partial => "partial",
            Outcome::Skipped => "skipped",
            Outcome::Rejected => "rejected",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_message_survives_a_round_trip() {
        let bodies = vec![
            Body::Hello(Hello {
                worker_id: "w1".into(),
                last_acked: 7,
                wallet: "0xabc".into(),
            }),
            Body::Heartbeat(Heartbeat {
                unix_ms: 1,
                orders_sent: 2,
                fills_seen: 3,
                last_acked: 4,
            }),
            Body::Command(PositionDelta {
                token_id: "123".into(),
                leader_before: -5,
                leader_after: 9,
                leader_price: "0.52".into(),
            }),
            Body::Ack(Ack {
                acked_seq: 9,
                outcome: Outcome::Partial as i32,
                detail: "x".into(),
            }),
            Body::Replay(ReplayRequest { after_seq: 3 }),
        ];
        for (i, b) in bodies.into_iter().enumerate() {
            let bytes = encode(i as u64 + 1, b.clone());
            let e = decode(&bytes).unwrap();
            assert_eq!(e.seq, i as u64 + 1);
            assert_eq!(e.body, Some(b));
        }
    }

    #[test]
    fn garbage_does_not_decode_into_a_frame_with_a_body() {
        let junk = [0xffu8, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff];
        assert!(decode(&junk).is_err());
    }

    #[test]
    fn a_negative_position_uses_the_signed_encoding() {
        let bytes = encode(
            1,
            Body::Command(PositionDelta {
                token_id: "t".into(),
                leader_before: i64::MIN,
                leader_after: i64::MAX,
                leader_price: String::new(),
            }),
        );
        let Some(Body::Command(c)) = decode(&bytes).unwrap().body else {
            panic!("not a command")
        };
        assert_eq!((c.leader_before, c.leader_after), (i64::MIN, i64::MAX));
    }
}
