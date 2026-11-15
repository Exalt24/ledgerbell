//! EIP-712 typed data hashing and signing, written from the EIP-712 text (https://eips.ethereum.org/EIPS/eip-712)
//! and checked against the Mail example in that EIP and its Example.js (see the tests).
//!
//! Supported member types: `uintN`, `address`, `bool`, `bytesN`, `bytes`, `string` and nested structs.
//! Arrays are not supported and are refused, not guessed at. The hash is Keccak-256 (the `sha3` crate); the
//! signature is secp256k1 ECDSA with a recovery id (the `k256` crate), RFC 6979 nonces, low `s`.

use std::collections::BTreeMap;

use k256::ecdsa::{RecoveryId, Signature, SigningKey, VerifyingKey};
use sha3::{Digest, Keccak256};

pub fn keccak256(data: &[u8]) -> [u8; 32] {
    let mut h = Keccak256::new();
    h.update(data);
    h.finalize().into()
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Eip712Error {
    UnknownType(String),
    MissingField { ty: String, field: String },
    WrongValue { ty: String, field: String, want: String },
    Unsupported(String),
    BadHex(String),
    BadNumber(String),
    BadSignature(String),
}

impl std::fmt::Display for Eip712Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Eip712Error::UnknownType(t) => write!(f, "unknown type {t}"),
            Eip712Error::MissingField { ty, field } => write!(f, "{ty} has no value for {field}"),
            Eip712Error::WrongValue { ty, field, want } => write!(f, "{ty}.{field}: expected {want}"),
            Eip712Error::Unsupported(t) => write!(f, "type {t} is not supported"),
            Eip712Error::BadHex(s) => write!(f, "bad hex: {s}"),
            Eip712Error::BadNumber(s) => write!(f, "bad number: {s}"),
            Eip712Error::BadSignature(s) => write!(f, "bad signature: {s}"),
        }
    }
}

impl std::error::Error for Eip712Error {}

pub type Word = [u8; 32];

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Value {
    /// An unsigned integer, 32 bytes big endian.
    Uint(Word),
    Address([u8; 20]),
    Bool(bool),
    /// `bytesN` and `bytes`: the raw bytes.
    Bytes(Vec<u8>),
    String(String),
    Struct(BTreeMap<String, Value>),
}

impl Value {
    pub fn uint(n: u128) -> Value {
        let mut w = [0u8; 32];
        w[16..].copy_from_slice(&n.to_be_bytes());
        Value::Uint(w)
    }

    /// A decimal string of any length up to 78 digits (the size of a uint256).
    pub fn uint_decimal(s: &str) -> Result<Value, Eip712Error> {
        if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
            return Err(Eip712Error::BadNumber(s.to_string()));
        }
        let mut w = [0u8; 32];
        for b in s.bytes() {
            let mut carry = (b - b'0') as u32;
            for byte in w.iter_mut().rev() {
                let v = *byte as u32 * 10 + carry;
                *byte = (v & 0xff) as u8;
                carry = v >> 8;
            }
            if carry != 0 {
                return Err(Eip712Error::BadNumber(format!("{s} does not fit in 256 bits")));
            }
        }
        Ok(Value::Uint(w))
    }

    pub fn address(s: &str) -> Result<Value, Eip712Error> {
        Ok(Value::Address(parse_address(s)?))
    }

    pub fn strukt(fields: impl IntoIterator<Item = (&'static str, Value)>) -> Value {
        Value::Struct(fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }
}

pub fn parse_address(s: &str) -> Result<[u8; 20], Eip712Error> {
    let h = s.strip_prefix("0x").unwrap_or(s);
    let v = hex_decode(h)?;
    v.try_into()
        .map_err(|_| Eip712Error::BadHex(format!("{s} is not 20 bytes")))
}

pub fn hex_decode(s: &str) -> Result<Vec<u8>, Eip712Error> {
    if !s.len().is_multiple_of(2) {
        return Err(Eip712Error::BadHex(format!("odd length: {s}")));
    }
    let digit = |c: u8| -> Result<u8, Eip712Error> {
        match c {
            b'0'..=b'9' => Ok(c - b'0'),
            b'a'..=b'f' => Ok(c - b'a' + 10),
            b'A'..=b'F' => Ok(c - b'A' + 10),
            _ => Err(Eip712Error::BadHex(s.to_string())),
        }
    };
    s.as_bytes()
        .chunks(2)
        .map(|p| Ok(digit(p[0])? << 4 | digit(p[1])?))
        .collect()
}

pub fn hex_encode(b: &[u8]) -> String {
    const D: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push(D[(x >> 4) as usize] as char);
        s.push(D[(x & 15) as usize] as char);
    }
    s
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Field {
    pub name: String,
    pub ty: String,
}

pub fn field(name: &str, ty: &str) -> Field {
    Field {
        name: name.to_string(),
        ty: ty.to_string(),
    }
}

#[derive(Clone, Debug)]
pub struct TypedData {
    pub types: BTreeMap<String, Vec<Field>>,
    pub primary_type: String,
    pub domain: Value,
    pub message: Value,
}

impl TypedData {
    /// `encodeType`: the primary type first, then every type it uses, sorted by name.
    pub fn encode_type(&self, primary: &str) -> Result<String, Eip712Error> {
        let mut found: Vec<String> = Vec::new();
        self.dependencies(primary, &mut found)?;
        let mut rest: Vec<&String> = found.iter().filter(|t| t.as_str() != primary).collect();
        rest.sort();
        let mut out = String::new();
        for t in std::iter::once(&primary.to_string()).chain(rest.into_iter().cloned().collect::<Vec<_>>().iter()) {
            let fields = self.types.get(t).ok_or_else(|| Eip712Error::UnknownType(t.clone()))?;
            let members: Vec<String> = fields.iter().map(|f| format!("{} {}", f.ty, f.name)).collect();
            out.push_str(&format!("{t}({})", members.join(",")));
        }
        Ok(out)
    }

    fn dependencies(&self, ty: &str, found: &mut Vec<String>) -> Result<(), Eip712Error> {
        if found.iter().any(|t| t == ty) {
            return Ok(());
        }
        let Some(fields) = self.types.get(ty) else {
            return Err(Eip712Error::UnknownType(ty.to_string()));
        };
        found.push(ty.to_string());
        for f in fields {
            if f.ty.ends_with(']') {
                return Err(Eip712Error::Unsupported(f.ty.clone()));
            }
            if self.types.contains_key(&f.ty) {
                self.dependencies(&f.ty, found)?;
            }
        }
        Ok(())
    }

    pub fn type_hash(&self, ty: &str) -> Result<Word, Eip712Error> {
        Ok(keccak256(self.encode_type(ty)?.as_bytes()))
    }

    /// `encodeData` preceded by the type hash.
    pub fn encode_data(&self, ty: &str, value: &Value) -> Result<Vec<u8>, Eip712Error> {
        let Value::Struct(map) = value else {
            return Err(Eip712Error::WrongValue {
                ty: ty.to_string(),
                field: "(whole value)".into(),
                want: "a struct".into(),
            });
        };
        let fields = self
            .types
            .get(ty)
            .ok_or_else(|| Eip712Error::UnknownType(ty.to_string()))?;
        let mut out = Vec::with_capacity(32 * (fields.len() + 1));
        out.extend_from_slice(&self.type_hash(ty)?);
        for f in fields {
            let v = map.get(&f.name).ok_or_else(|| Eip712Error::MissingField {
                ty: ty.to_string(),
                field: f.name.clone(),
            })?;
            out.extend_from_slice(&self.encode_member(ty, f, v)?);
        }
        Ok(out)
    }

    fn encode_member(&self, owner: &str, f: &Field, v: &Value) -> Result<Word, Eip712Error> {
        let wrong = |want: &str| Eip712Error::WrongValue {
            ty: owner.to_string(),
            field: f.name.clone(),
            want: want.to_string(),
        };
        if f.ty.ends_with(']') {
            return Err(Eip712Error::Unsupported(f.ty.clone()));
        }
        if self.types.contains_key(&f.ty) {
            return Ok(keccak256(&self.encode_data(&f.ty, v)?));
        }
        match (f.ty.as_str(), v) {
            ("string", Value::String(s)) => Ok(keccak256(s.as_bytes())),
            ("bytes", Value::Bytes(b)) => Ok(keccak256(b)),
            ("address", Value::Address(a)) => {
                let mut w = [0u8; 32];
                w[12..].copy_from_slice(a);
                Ok(w)
            }
            ("bool", Value::Bool(b)) => {
                let mut w = [0u8; 32];
                w[31] = *b as u8;
                Ok(w)
            }
            (t, Value::Uint(w)) if t.starts_with("uint") => {
                let bits: usize = t[4..].parse().map_err(|_| Eip712Error::Unsupported(t.to_string()))?;
                if bits == 0 || bits > 256 || !bits.is_multiple_of(8) {
                    return Err(Eip712Error::Unsupported(t.to_string()));
                }
                let spare = (256 - bits) / 8;
                if w[..spare].iter().any(|b| *b != 0) {
                    return Err(wrong(&format!("a value that fits in {bits} bits")));
                }
                Ok(*w)
            }
            (t, Value::Bytes(b)) if t.starts_with("bytes") => {
                let n: usize = t[5..].parse().map_err(|_| Eip712Error::Unsupported(t.to_string()))?;
                if n == 0 || n > 32 {
                    return Err(Eip712Error::Unsupported(t.to_string()));
                }
                if b.len() != n {
                    return Err(wrong(&format!("{n} bytes")));
                }
                let mut w = [0u8; 32];
                w[..n].copy_from_slice(b);
                Ok(w)
            }
            (t, _) => Err(wrong(&format!("a value for type {t}"))),
        }
    }

    pub fn hash_struct(&self, ty: &str, value: &Value) -> Result<Word, Eip712Error> {
        Ok(keccak256(&self.encode_data(ty, value)?))
    }

    pub fn domain_separator(&self) -> Result<Word, Eip712Error> {
        self.hash_struct("EIP712Domain", &self.domain)
    }

    /// `keccak256("\x19\x01" || domainSeparator || hashStruct(message))`: the 32 bytes that are signed.
    pub fn signing_digest(&self) -> Result<Word, Eip712Error> {
        let mut buf = Vec::with_capacity(66);
        buf.extend_from_slice(&[0x19, 0x01]);
        buf.extend_from_slice(&self.domain_separator()?);
        buf.extend_from_slice(&self.hash_struct(&self.primary_type, &self.message)?);
        Ok(keccak256(&buf))
    }
}

/// A secp256k1 key. Tests use [`Wallet::throwaway`]; nothing in this crate reads a key from anywhere else
/// except a file the caller names.
pub struct Wallet {
    key: SigningKey,
}

impl Wallet {
    pub fn from_secret(secret: &[u8; 32]) -> Result<Wallet, Eip712Error> {
        SigningKey::from_slice(secret)
            .map(|key| Wallet { key })
            .map_err(|e| Eip712Error::BadSignature(e.to_string()))
    }

    /// A key from the operating system's random source. It exists only for the life of the process (or the
    /// file the caller writes it to).
    pub fn throwaway() -> (Wallet, [u8; 32]) {
        loop {
            let mut s = [0u8; 32];
            getrandom::fill(&mut s).expect("operating system random bytes");
            if let Ok(w) = Wallet::from_secret(&s) {
                return (w, s);
            }
        }
    }

    pub fn address(&self) -> [u8; 20] {
        address_of(self.key.verifying_key())
    }

    pub fn address_hex(&self) -> String {
        format!("0x{}", hex_encode(&self.address()))
    }

    /// 65 bytes: r, s, then v as 27 or 28 (the form `eth_signTypedData` returns).
    pub fn sign_digest(&self, digest: &Word) -> Result<[u8; 65], Eip712Error> {
        let (sig, rid) = self
            .key
            .sign_prehash_recoverable(digest)
            .map_err(|e| Eip712Error::BadSignature(e.to_string()))?;
        let mut out = [0u8; 65];
        out[..64].copy_from_slice(&sig.to_bytes());
        out[64] = 27 + rid.to_byte();
        Ok(out)
    }

    pub fn sign_typed(&self, data: &TypedData) -> Result<[u8; 65], Eip712Error> {
        self.sign_digest(&data.signing_digest()?)
    }
}

fn address_of(vk: &VerifyingKey) -> [u8; 20] {
    let point = vk.to_encoded_point(false);
    let h = keccak256(&point.as_bytes()[1..]);
    let mut a = [0u8; 20];
    a.copy_from_slice(&h[12..]);
    a
}

/// The address that signed `digest` with `sig` (65 bytes, v of 27 or 28, or 0 or 1).
pub fn recover_address(digest: &Word, sig: &[u8]) -> Result<[u8; 20], Eip712Error> {
    if sig.len() != 65 {
        return Err(Eip712Error::BadSignature(format!("{} bytes, expected 65", sig.len())));
    }
    let v = match sig[64] {
        27 | 28 => sig[64] - 27,
        0 | 1 => sig[64],
        other => return Err(Eip712Error::BadSignature(format!("v is {other}"))),
    };
    let rid = RecoveryId::from_byte(v).ok_or_else(|| Eip712Error::BadSignature("recovery id".into()))?;
    let s = Signature::from_slice(&sig[..64]).map_err(|e| Eip712Error::BadSignature(e.to_string()))?;
    let vk =
        VerifyingKey::recover_from_prehash(digest, &s, rid).map_err(|e| Eip712Error::BadSignature(e.to_string()))?;
    Ok(address_of(&vk))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn h(s: &str) -> Vec<u8> {
        hex_decode(s.strip_prefix("0x").unwrap_or(s)).unwrap()
    }

    /// The Mail example from the EIP-712 text and its Example.js.
    pub(crate) fn mail() -> TypedData {
        let mut types = BTreeMap::new();
        types.insert(
            "EIP712Domain".to_string(),
            vec![
                field("name", "string"),
                field("version", "string"),
                field("chainId", "uint256"),
                field("verifyingContract", "address"),
            ],
        );
        types.insert(
            "Person".to_string(),
            vec![field("name", "string"), field("wallet", "address")],
        );
        types.insert(
            "Mail".to_string(),
            vec![
                field("from", "Person"),
                field("to", "Person"),
                field("contents", "string"),
            ],
        );
        TypedData {
            types,
            primary_type: "Mail".into(),
            domain: Value::strukt([
                ("name", Value::String("Ether Mail".into())),
                ("version", Value::String("1".into())),
                ("chainId", Value::uint(1)),
                (
                    "verifyingContract",
                    Value::address("0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC").unwrap(),
                ),
            ]),
            message: Value::strukt([
                (
                    "from",
                    Value::strukt([
                        ("name", Value::String("Cow".into())),
                        (
                            "wallet",
                            Value::address("0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826").unwrap(),
                        ),
                    ]),
                ),
                (
                    "to",
                    Value::strukt([
                        ("name", Value::String("Bob".into())),
                        (
                            "wallet",
                            Value::address("0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB").unwrap(),
                        ),
                    ]),
                ),
                ("contents", Value::String("Hello, Bob!".into())),
            ]),
        }
    }

    #[test]
    fn encode_type_matches_the_example() {
        assert_eq!(
            mail().encode_type("Mail").unwrap(),
            "Mail(Person from,Person to,string contents)Person(string name,address wallet)"
        );
    }

    #[test]
    fn type_hash_matches_the_example() {
        assert_eq!(
            hex_encode(&mail().type_hash("Mail").unwrap()),
            "a0cedeb2dc280ba39b857546d74f5549c3a1d7bdc2dd96bf881f76108e23dac2"
        );
    }

    #[test]
    fn encode_data_matches_the_example() {
        let t = mail();
        assert_eq!(
            hex_encode(&t.encode_data("Mail", &t.message).unwrap()),
            "a0cedeb2dc280ba39b857546d74f5549c3a1d7bdc2dd96bf881f76108e23dac2fc71e5fa27ff56c350aa531bc129ebdf613b772b6604664f5d8dbe21b85eb0c8cd54f074a4af31b4411ff6a60c9719dbd559c221c8ac3492d9d872b041d703d1b5aadf3154a261abdd9086fc627b61efca26ae5702701d05cd2305f7c52a2fc8"
        );
    }

    #[test]
    fn struct_hash_domain_separator_and_digest_match_the_example() {
        let t = mail();
        assert_eq!(
            hex_encode(&t.hash_struct("Mail", &t.message).unwrap()),
            "c52c0ee5d84264471806290a3f2c4cecfc5490626bf912d01f240d7a274b371e"
        );
        assert_eq!(
            hex_encode(&t.domain_separator().unwrap()),
            "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f"
        );
        assert_eq!(
            hex_encode(&t.signing_digest().unwrap()),
            "be609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2"
        );
    }

    #[test]
    fn the_key_keccak_of_cow_has_the_examples_address_and_signature() {
        let secret = keccak256(b"cow");
        let w = Wallet::from_secret(&secret).unwrap();
        assert_eq!(w.address_hex(), "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826");
        let sig = w.sign_typed(&mail()).unwrap();
        // The EIP's eth_signTypedData result, and Example.js: v 28, r and s as stated.
        assert_eq!(
            hex_encode(&sig),
            "4355c47d63924e8a72e509b65029052eb6c299d53a04e167c5775fd466751c9d07299936d304c153f6443dfa05f40ff007d72911b6f72307f996231605b915621c"
        );
        assert_eq!(sig[64], 28);
        assert_eq!(
            &sig[..32],
            h("4355c47d63924e8a72e509b65029052eb6c299d53a04e167c5775fd466751c9d").as_slice()
        );
        assert_eq!(
            &sig[32..64],
            h("07299936d304c153f6443dfa05f40ff007d72911b6f72307f996231605b91562").as_slice()
        );
    }

    #[test]
    fn the_example_signature_recovers_to_the_example_address() {
        let t = mail();
        let sig = h("4355c47d63924e8a72e509b65029052eb6c299d53a04e167c5775fd466751c9d07299936d304c153f6443dfa05f40ff007d72911b6f72307f996231605b915621c");
        let a = recover_address(&t.signing_digest().unwrap(), &sig).unwrap();
        assert_eq!(
            format!("0x{}", hex_encode(&a)),
            "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826"
        );
    }

    #[test]
    fn changing_the_message_changes_the_digest_and_breaks_recovery() {
        let t = mail();
        let w = Wallet::from_secret(&keccak256(b"cow")).unwrap();
        let sig = w.sign_typed(&t).unwrap();
        let mut t2 = t.clone();
        if let Value::Struct(m) = &mut t2.message {
            m.insert("contents".into(), Value::String("Hello, Bob?".into()));
        }
        assert_ne!(t.signing_digest().unwrap(), t2.signing_digest().unwrap());
        let a = recover_address(&t2.signing_digest().unwrap(), &sig).unwrap();
        assert_ne!(a, w.address());
    }

    #[test]
    fn a_tampered_signature_does_not_recover_the_signer() {
        let t = mail();
        let w = Wallet::from_secret(&keccak256(b"cow")).unwrap();
        let mut sig = w.sign_typed(&t).unwrap();
        sig[10] ^= 1;
        if let Ok(a) = recover_address(&t.signing_digest().unwrap(), &sig) {
            assert_ne!(a, w.address());
        }
    }

    #[test]
    fn a_wrong_length_or_v_is_refused() {
        let d = [1u8; 32];
        assert!(recover_address(&d, &[0u8; 64]).is_err());
        let mut sig = [1u8; 65];
        sig[64] = 31;
        assert!(recover_address(&d, &sig).is_err());
    }

    #[test]
    fn unknown_types_missing_fields_and_arrays_are_refused() {
        let mut t = mail();
        assert_eq!(t.encode_type("Nope"), Err(Eip712Error::UnknownType("Nope".into())));
        if let Value::Struct(m) = &mut t.message {
            m.remove("contents");
        }
        assert!(matches!(
            t.hash_struct("Mail", &t.message),
            Err(Eip712Error::MissingField { .. })
        ));
        let mut t = mail();
        t.types.get_mut("Mail").unwrap().push(field("list", "uint256[]"));
        assert!(matches!(t.encode_type("Mail"), Err(Eip712Error::Unsupported(_))));
    }

    #[test]
    fn a_value_of_the_wrong_kind_is_refused() {
        let mut t = mail();
        if let Value::Struct(m) = &mut t.message {
            m.insert("contents".into(), Value::Bool(true));
        }
        assert!(matches!(
            t.hash_struct("Mail", &t.message),
            Err(Eip712Error::WrongValue { .. })
        ));
    }

    fn one_field(ty: &str, v: Value) -> Result<Word, Eip712Error> {
        let mut types = BTreeMap::new();
        types.insert("T".to_string(), vec![field("x", ty)]);
        let t = TypedData {
            types,
            primary_type: "T".into(),
            domain: Value::strukt([]),
            message: Value::strukt([("x", v)]),
        };
        t.hash_struct("T", &t.message)
    }

    #[test]
    fn a_uint_must_fit_its_declared_width() {
        assert!(one_field("uint8", Value::uint(255)).is_ok());
        assert!(matches!(
            one_field("uint8", Value::uint(256)),
            Err(Eip712Error::WrongValue { .. })
        ));
        assert!(one_field("uint256", Value::uint_decimal(&"9".repeat(77)).unwrap()).is_ok());
        assert!(matches!(
            one_field("uint7", Value::uint(1)),
            Err(Eip712Error::Unsupported(_))
        ));
        assert!(matches!(
            one_field("uint264", Value::uint(1)),
            Err(Eip712Error::Unsupported(_))
        ));
    }

    #[test]
    fn fixed_bytes_must_have_the_declared_length() {
        assert!(one_field("bytes4", Value::Bytes(vec![1, 2, 3, 4])).is_ok());
        assert!(matches!(
            one_field("bytes4", Value::Bytes(vec![1, 2, 3])),
            Err(Eip712Error::WrongValue { .. })
        ));
        assert!(matches!(
            one_field("bytes33", Value::Bytes(vec![0; 33])),
            Err(Eip712Error::Unsupported(_))
        ));
    }

    #[test]
    fn a_bool_encodes_as_zero_or_one() {
        let a = one_field("bool", Value::Bool(true)).unwrap();
        let b = one_field("bool", Value::Bool(false)).unwrap();
        assert_ne!(a, b);
    }

    #[test]
    fn decimal_numbers_parse_exactly_and_overflow_is_refused() {
        assert_eq!(Value::uint_decimal("0").unwrap(), Value::uint(0));
        assert_eq!(
            Value::uint_decimal("340282366920938463463374607431768211455").unwrap(),
            Value::uint(u128::MAX)
        );
        // 2^256 - 1 is 78 digits; one more does not fit.
        let max = "115792089237316195423570985008687907853269984665640564039457584007913129639935";
        assert!(Value::uint_decimal(max).is_ok());
        let over = "115792089237316195423570985008687907853269984665640564039457584007913129639936";
        assert!(matches!(Value::uint_decimal(over), Err(Eip712Error::BadNumber(_))));
        assert!(Value::uint_decimal("").is_err());
        assert!(Value::uint_decimal("12a").is_err());
    }

    #[test]
    fn hex_round_trips_and_rejects_bad_input() {
        assert_eq!(hex_encode(&hex_decode("00ff10Ab").unwrap()), "00ff10ab");
        assert!(hex_decode("abc").is_err());
        assert!(hex_decode("zz").is_err());
        assert!(parse_address("0x1234").is_err());
    }

    #[test]
    fn a_throwaway_key_signs_and_its_address_is_recovered() {
        let (w, _) = Wallet::throwaway();
        let d = keccak256(b"anything");
        let sig = w.sign_digest(&d).unwrap();
        assert_eq!(recover_address(&d, &sig).unwrap(), w.address());
        let (w2, _) = Wallet::throwaway();
        assert_ne!(w.address(), w2.address());
    }
}
