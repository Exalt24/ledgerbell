//! Sequence ids. A stream of commands is numbered 1, 2, 3 ... with no holes, and the receiver keeps the
//! highest id it has fully handled (`last_acked`). One pure function says what to do with the next frame.

/// What a receiver does with a frame, given the highest id it has already handled.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SeqVerdict {
    /// It is exactly the next id: handle it.
    Apply,
    /// Its id is at or below the last handled one: drop it, it was handled before.
    Duplicate,
    /// An id was skipped. `expected` is the one that should have come: ask for a replay after `expected - 1`.
    Gap { expected: u64 },
    /// Id 0 is never used.
    Invalid,
}

pub fn classify(last_acked: u64, incoming: u64) -> SeqVerdict {
    if incoming == 0 {
        return SeqVerdict::Invalid;
    }
    // At the largest id there is no next one, so every frame is old.
    let Some(expected) = last_acked.checked_add(1) else {
        return SeqVerdict::Duplicate;
    };
    match incoming.cmp(&expected) {
        std::cmp::Ordering::Equal => SeqVerdict::Apply,
        std::cmp::Ordering::Less => SeqVerdict::Duplicate,
        std::cmp::Ordering::Greater => SeqVerdict::Gap { expected },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_next_id_applies() {
        assert_eq!(classify(0, 1), SeqVerdict::Apply);
        assert_eq!(classify(41, 42), SeqVerdict::Apply);
    }

    #[test]
    fn an_old_or_repeated_id_is_a_duplicate() {
        assert_eq!(classify(5, 5), SeqVerdict::Duplicate);
        assert_eq!(classify(5, 1), SeqVerdict::Duplicate);
    }

    #[test]
    fn a_skipped_id_is_a_gap_that_names_the_missing_one() {
        assert_eq!(classify(5, 7), SeqVerdict::Gap { expected: 6 });
        assert_eq!(classify(0, 9), SeqVerdict::Gap { expected: 1 });
    }

    #[test]
    fn id_zero_is_invalid_whatever_came_before() {
        assert_eq!(classify(0, 0), SeqVerdict::Invalid);
        assert_eq!(classify(9, 0), SeqVerdict::Invalid);
    }

    #[test]
    fn the_highest_possible_id_does_not_overflow() {
        assert_eq!(classify(u64::MAX, u64::MAX), SeqVerdict::Duplicate);
        assert_eq!(classify(u64::MAX - 1, u64::MAX), SeqVerdict::Apply);
    }
}
