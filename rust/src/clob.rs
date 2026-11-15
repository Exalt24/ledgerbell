//! A Polymarket CLOB order, its EIP-712 signature, and a stand-in CLOB to send it to.
//!
//! Everything here is written from Polymarket's public documentation (docs.polymarket.com, read on
//! 2026-11-15; the lines used are quoted in docs/clob-contract.md). It was never pointed at the live service.
//! The documentation names the signed order's fields and their order, the domain and the REST routes, but not
//! the Solidity types of each field, so those types are this crate's reading (see `ORDER_TYPES`). A signature
//! made here is therefore not claimed to be valid on the real exchange.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value as Json_};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};

use crate::book::{Fixed, Level};
use crate::eip712::{
    field, hex_decode, hex_encode, parse_address, recover_address, Eip712Error, TypedData, Value, Wallet, Word,
};
use crate::follow::OrderSide;

/// From the "Sign and Submit the V2 Order" step of the Protocol V2 API migration page.
pub const DOMAIN_NAME: &str = "Polymarket CTF Exchange";
pub const DOMAIN_VERSION: &str = "3";
pub const CHAIN_ID: u128 = 137;
pub const VERIFYING_CONTRACT: &str = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa";

/// The signed order struct's fields, in the order the Builder Fees page lists them: `salt, maker, signer,
/// tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder`. The Solidity types
/// are this crate's reading: uint256 for numbers, uint8 for `side` and `signatureType`, bytes32 for `metadata`
/// and `builder`. `expiration` is on the wire but not in the signed struct (the OpenAPI schema says so).
pub const ORDER_TYPES: [(&str, &str); 11] = [
    ("salt", "uint256"),
    ("maker", "address"),
    ("signer", "address"),
    ("tokenId", "uint256"),
    ("makerAmount", "uint256"),
    ("takerAmount", "uint256"),
    ("side", "uint8"),
    ("signatureType", "uint8"),
    ("timestamp", "uint256"),
    ("metadata", "bytes32"),
    ("builder", "bytes32"),
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Order {
    pub salt: u64,
    pub maker: [u8; 20],
    pub signer: [u8; 20],
    /// The outcome token (asset) id as a decimal string.
    pub token_id: String,
    /// In 6 decimal base units: collateral for a buy, shares for a sell.
    pub maker_amount: u64,
    /// In 6 decimal base units: shares for a buy, collateral for a sell.
    pub taker_amount: u64,
    pub side: OrderSide,
    /// 0 is an EOA signing for itself.
    pub signature_type: u8,
    pub timestamp_ms: u64,
    pub metadata: [u8; 32],
    pub builder: [u8; 32],
    /// On the wire only. 0 means none.
    pub expiration: u64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum OrderError {
    Sign(Eip712Error),
    /// The size or price rounds to nothing in 6 decimals.
    Amounts(String),
    Wire(String),
}

impl std::fmt::Display for OrderError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OrderError::Sign(e) => write!(f, "{e}"),
            OrderError::Amounts(s) => write!(f, "{s}"),
            OrderError::Wire(s) => write!(f, "bad order: {s}"),
        }
    }
}

impl std::error::Error for OrderError {}

impl From<Eip712Error> for OrderError {
    fn from(e: Eip712Error) -> Self {
        OrderError::Sign(e)
    }
}

/// Turns a size in shares and a limit price (both 8 decimals) into the order's two amounts in 6 decimals.
/// Shares round down. Collateral rounds down for a buy and up for a sell, so the price the amounts imply is
/// never worse than `limit`.
pub fn amounts(side: OrderSide, qty: Fixed, limit: Fixed) -> Result<(u64, u64), OrderError> {
    let shares = qty.0 / 100;
    if shares == 0 || limit.is_zero() {
        return Err(OrderError::Amounts(format!("size {qty} at {limit} rounds to nothing")));
    }
    let notional = shares as u128 * limit.0 as u128;
    let scale = 100_000_000u128;
    let collateral = match side {
        OrderSide::Buy => notional / scale,
        OrderSide::Sell => notional.div_ceil(scale),
    } as u64;
    if collateral == 0 {
        return Err(OrderError::Amounts(format!("size {qty} at {limit} costs nothing")));
    }
    Ok(match side {
        OrderSide::Buy => (collateral, shares),
        OrderSide::Sell => (shares, collateral),
    })
}

fn side_str(s: OrderSide) -> &'static str {
    match s {
        OrderSide::Buy => "BUY",
        OrderSide::Sell => "SELL",
    }
}

impl Order {
    pub fn typed_data(&self) -> Result<TypedData, OrderError> {
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
            "Order".to_string(),
            ORDER_TYPES.iter().map(|(n, t)| field(n, t)).collect(),
        );
        Ok(TypedData {
            types,
            primary_type: "Order".into(),
            domain: Value::strukt([
                ("name", Value::String(DOMAIN_NAME.into())),
                ("version", Value::String(DOMAIN_VERSION.into())),
                ("chainId", Value::uint(CHAIN_ID)),
                ("verifyingContract", Value::address(VERIFYING_CONTRACT)?),
            ]),
            message: Value::strukt([
                ("salt", Value::uint(self.salt as u128)),
                ("maker", Value::Address(self.maker)),
                ("signer", Value::Address(self.signer)),
                ("tokenId", Value::uint_decimal(&self.token_id)?),
                ("makerAmount", Value::uint(self.maker_amount as u128)),
                ("takerAmount", Value::uint(self.taker_amount as u128)),
                (
                    "side",
                    Value::uint(match self.side {
                        OrderSide::Buy => 0,
                        OrderSide::Sell => 1,
                    }),
                ),
                ("signatureType", Value::uint(self.signature_type as u128)),
                ("timestamp", Value::uint(self.timestamp_ms as u128)),
                ("metadata", Value::Bytes(self.metadata.to_vec())),
                ("builder", Value::Bytes(self.builder.to_vec())),
            ]),
        })
    }

    pub fn digest(&self) -> Result<Word, OrderError> {
        Ok(self.typed_data()?.signing_digest()?)
    }

    pub fn sign(&self, wallet: &Wallet) -> Result<[u8; 65], OrderError> {
        Ok(wallet.sign_digest(&self.digest()?)?)
    }

    /// The JSON body of `POST /order`, in the shape of the OpenAPI schema (`SendOrder`).
    pub fn to_wire(&self, signature: &[u8; 65]) -> Json_ {
        json!({
            "order": {
                "maker": format!("0x{}", hex_encode(&self.maker)),
                "signer": format!("0x{}", hex_encode(&self.signer)),
                "tokenId": self.token_id,
                "makerAmount": self.maker_amount.to_string(),
                "takerAmount": self.taker_amount.to_string(),
                "side": side_str(self.side),
                "expiration": self.expiration.to_string(),
                "timestamp": self.timestamp_ms.to_string(),
                "metadata": if self.metadata == [0u8; 32] { String::new() } else { format!("0x{}", hex_encode(&self.metadata)) },
                "builder": format!("0x{}", hex_encode(&self.builder)),
                "signature": format!("0x{}", hex_encode(signature)),
                "salt": self.salt,
                "signatureType": self.signature_type,
            },
            "owner": "stand-in-owner",
            "orderType": "FAK",
            "deferExec": false,
            "postOnly": false,
        })
    }

    pub fn from_wire(body: &Json_) -> Result<(Order, Vec<u8>), OrderError> {
        let o = body.get("order").ok_or_else(|| OrderError::Wire("no order".into()))?;
        let s = |k: &str| -> Result<&str, OrderError> {
            o.get(k)
                .and_then(|v| v.as_str())
                .ok_or_else(|| OrderError::Wire(format!("{k} is missing or not a string")))
        };
        let n = |k: &str| -> Result<u64, OrderError> {
            s(k)?
                .parse()
                .map_err(|_| OrderError::Wire(format!("{k} is not a number")))
        };
        let side = match s("side")? {
            "BUY" => OrderSide::Buy,
            "SELL" => OrderSide::Sell,
            other => return Err(OrderError::Wire(format!("side {other}"))),
        };
        let b32 = |k: &str| -> Result<[u8; 32], OrderError> {
            let v = s(k)?;
            if v.is_empty() {
                return Ok([0u8; 32]);
            }
            let raw = hex_decode(v.strip_prefix("0x").unwrap_or(v))?;
            raw.try_into()
                .map_err(|_| OrderError::Wire(format!("{k} is not 32 bytes")))
        };
        let order = Order {
            salt: o
                .get("salt")
                .and_then(|v| v.as_u64())
                .ok_or_else(|| OrderError::Wire("salt is not an integer".into()))?,
            maker: parse_address(s("maker")?)?,
            signer: parse_address(s("signer")?)?,
            token_id: s("tokenId")?.to_string(),
            maker_amount: n("makerAmount")?,
            taker_amount: n("takerAmount")?,
            side,
            signature_type: o
                .get("signatureType")
                .and_then(|v| v.as_u64())
                .and_then(|v| u8::try_from(v).ok())
                .ok_or_else(|| OrderError::Wire("signatureType".into()))?,
            timestamp_ms: n("timestamp")?,
            metadata: b32("metadata")?,
            builder: b32("builder")?,
            expiration: n("expiration")?,
        };
        let sig = s("signature")?;
        let sig = hex_decode(sig.strip_prefix("0x").unwrap_or(sig))?;
        Ok((order, sig))
    }

    /// The number of shares the order is for.
    pub fn shares(&self) -> Fixed {
        let base = match self.side {
            OrderSide::Buy => self.taker_amount,
            OrderSide::Sell => self.maker_amount,
        };
        Fixed(base * 100)
    }

    /// The price the amounts imply, 8 decimals, rounded toward the side's worse end.
    pub fn implied_price(&self) -> Fixed {
        let (collateral, shares) = match self.side {
            OrderSide::Buy => (self.maker_amount, self.taker_amount),
            OrderSide::Sell => (self.taker_amount, self.maker_amount),
        };
        if shares == 0 {
            return Fixed::ZERO;
        }
        let num = collateral as u128 * 100_000_000;
        Fixed(match self.side {
            OrderSide::Buy => num.div_ceil(shares as u128),
            OrderSide::Sell => num / shares as u128,
        } as u64)
    }
}

// ---------------------------------------------------------------- a minimal HTTP client

/// One plain `http://` request over a fresh TCP connection (`Connection: close`). It exists so the worker can
/// talk to the stand-in CLOB without an HTTP client crate; it does no TLS and is not meant for anything else.
pub async fn http_request(
    base: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
    timeout: Duration,
) -> Result<(u16, String), String> {
    let hostport = base
        .strip_prefix("http://")
        .ok_or("only http:// is supported")?
        .trim_end_matches('/');
    let fut = async {
        let mut s = TcpStream::connect(hostport)
            .await
            .map_err(|e| format!("connect: {e}"))?;
        let body = body.unwrap_or("");
        let req = format!(
            "{method} {path} HTTP/1.1\r\nHost: {hostport}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        s.write_all(req.as_bytes()).await.map_err(|e| format!("write: {e}"))?;
        let mut buf = Vec::new();
        s.read_to_end(&mut buf).await.map_err(|e| format!("read: {e}"))?;
        let text = String::from_utf8_lossy(&buf).into_owned();
        let (head, rest) = text.split_once("\r\n\r\n").ok_or("no header end")?;
        let status: u16 = head
            .split_whitespace()
            .nth(1)
            .and_then(|c| c.parse().ok())
            .ok_or("no status code")?;
        let chunked = head.to_ascii_lowercase().contains("transfer-encoding: chunked");
        let body = if chunked { dechunk(rest) } else { rest.to_string() };
        Ok((status, body))
    };
    tokio::time::timeout(timeout, fut)
        .await
        .map_err(|_| "timed out".to_string())?
}

fn dechunk(mut s: &str) -> String {
    let mut out = String::new();
    while let Some((len, rest)) = s.split_once("\r\n") {
        let n = usize::from_str_radix(len.trim(), 16).unwrap_or(0);
        if n == 0 || rest.len() < n {
            break;
        }
        out.push_str(&rest[..n]);
        s = rest[n..].trim_start_matches("\r\n");
    }
    out
}

// ---------------------------------------------------------------- the stand-in CLOB

#[derive(Clone, Debug)]
pub struct ClobConfig {
    /// How long a `POST /order` takes to answer.
    pub post_latency: Duration,
    /// Per poll of an order, the percent of it that reads as matched: poll `i` returns `plan[min(i, len - 1)]`.
    pub fill_plan: Vec<u32>,
}

impl Default for ClobConfig {
    fn default() -> Self {
        ClobConfig {
            post_latency: Duration::ZERO,
            fill_plan: vec![100],
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Received {
    pub id: String,
    pub maker: String,
    pub salt: u64,
    pub side: OrderSide,
    pub maker_amount: u64,
    pub taker_amount: u64,
    /// How many times this exact order (same hash) was posted.
    pub posts: u32,
}

struct Rec {
    order: Order,
    received: Received,
    polls: u32,
}

#[derive(Default)]
struct BookSide {
    bids: Vec<Level>,
    asks: Vec<Level>,
}

struct ClobState {
    cfg: Mutex<ClobConfig>,
    books: Mutex<HashMap<String, BookSide>>,
    orders: Mutex<HashMap<String, Rec>>,
    order_seq: Mutex<Vec<String>>,
    salts: Mutex<HashSet<(String, u64)>>,
    fail_posts: AtomicU32,
    refuse_posts: AtomicU32,
    refuse_with_ok_status: AtomicBool,
    posts_received: AtomicU64,
    post_times: Mutex<Vec<std::time::Instant>>,
    rejected: AtomicU64,
    polls: AtomicU64,
}

pub struct ClobStandIn {
    addr: SocketAddr,
    state: Arc<ClobState>,
    task: tokio::task::JoinHandle<()>,
}

fn fmt_level(l: &Level) -> Json_ {
    json!({"price": l.price.to_string(), "size": l.qty.to_string()})
}

impl ClobStandIn {
    pub async fn start(cfg: ClobConfig) -> std::io::Result<ClobStandIn> {
        Self::start_on("127.0.0.1:0".parse().unwrap(), cfg).await
    }

    pub async fn start_on(addr: SocketAddr, cfg: ClobConfig) -> std::io::Result<ClobStandIn> {
        let listener = TcpListener::bind(addr).await?;
        let addr = listener.local_addr()?;
        let state = Arc::new(ClobState {
            cfg: Mutex::new(cfg),
            books: Mutex::new(HashMap::new()),
            orders: Mutex::new(HashMap::new()),
            order_seq: Mutex::new(Vec::new()),
            salts: Mutex::new(HashSet::new()),
            fail_posts: AtomicU32::new(0),
            refuse_posts: AtomicU32::new(0),
            refuse_with_ok_status: AtomicBool::new(false),
            posts_received: AtomicU64::new(0),
            post_times: Mutex::new(Vec::new()),
            rejected: AtomicU64::new(0),
            polls: AtomicU64::new(0),
        });
        let app = Router::new()
            .route("/book", get(get_book))
            .route("/order", post(post_order))
            .route("/data/order/{id}", get(get_order))
            .with_state(state.clone());
        let task = tokio::spawn(async move {
            let _ = axum::serve(listener, app).await;
        });
        Ok(ClobStandIn { addr, state, task })
    }

    pub fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    /// Sets the book for a token. Bids are listed best (highest) first and asks best (lowest) first, as the
    /// OpenAPI page for `GET /book` says.
    pub fn set_book(&self, token_id: &str, bids: &[(&str, &str)], asks: &[(&str, &str)]) {
        let lv = |v: &[(&str, &str)]| -> Vec<Level> {
            v.iter()
                .map(|(p, q)| Level {
                    price: Fixed::parse(p).expect("price"),
                    qty: Fixed::parse(q).expect("size"),
                })
                .collect()
        };
        let mut bids = lv(bids);
        let mut asks = lv(asks);
        bids.sort_by_key(|l| std::cmp::Reverse(l.price));
        asks.sort_by_key(|l| l.price);
        self.state
            .books
            .lock()
            .unwrap()
            .insert(token_id.to_string(), BookSide { bids, asks });
    }

    pub fn set_fill_plan(&self, plan: Vec<u32>) {
        self.state.cfg.lock().unwrap().fill_plan = plan;
    }

    pub fn set_post_latency(&self, d: Duration) {
        self.state.cfg.lock().unwrap().post_latency = d;
    }

    /// The next `n` `POST /order` calls answer 500 and record nothing.
    pub fn fail_next_posts(&self, n: u32) {
        self.state.fail_posts.store(n, Ordering::SeqCst);
    }

    /// The next `n` `POST /order` calls are refused with `success: false` and an error message, with HTTP
    /// status 400, or 200 when `with_ok_status` is true. The order is not recorded.
    pub fn refuse_next_posts(&self, n: u32, with_ok_status: bool) {
        self.state.refuse_with_ok_status.store(with_ok_status, Ordering::SeqCst);
        self.state.refuse_posts.store(n, Ordering::SeqCst);
    }

    pub fn posts_received(&self) -> u64 {
        self.state.posts_received.load(Ordering::SeqCst)
    }

    /// The time between each `POST /order` and the one before it.
    pub fn post_gaps(&self) -> Vec<Duration> {
        let t = self.state.post_times.lock().unwrap();
        t.windows(2).map(|w| w[1].duration_since(w[0])).collect()
    }

    pub fn rejected(&self) -> u64 {
        self.state.rejected.load(Ordering::SeqCst)
    }

    pub fn polls(&self) -> u64 {
        self.state.polls.load(Ordering::SeqCst)
    }

    /// Distinct orders accepted, in the order they first arrived.
    pub fn received(&self) -> Vec<Received> {
        let orders = self.state.orders.lock().unwrap();
        let seq = self.state.order_seq.lock().unwrap();
        seq.iter().map(|id| orders[id].received.clone()).collect()
    }

    pub fn stop(&self) {
        self.task.abort();
    }
}

impl Drop for ClobStandIn {
    fn drop(&mut self) {
        self.task.abort();
    }
}

#[derive(serde::Deserialize)]
struct BookQuery {
    token_id: String,
}

async fn get_book(State(st): State<Arc<ClobState>>, Query(q): Query<BookQuery>) -> (StatusCode, Json<Json_>) {
    let books = st.books.lock().unwrap();
    match books.get(&q.token_id) {
        None => (
            StatusCode::NOT_FOUND,
            Json(json!({"error": "No orderbook exists for the requested token id"})),
        ),
        Some(b) => (
            StatusCode::OK,
            Json(json!({
                "market": "0x0000000000000000000000000000000000000000000000000000000000000001",
                "asset_id": q.token_id,
                "timestamp": "0",
                "hash": "stand-in",
                "bids": b.bids.iter().map(fmt_level).collect::<Vec<_>>(),
                "asks": b.asks.iter().map(fmt_level).collect::<Vec<_>>(),
                "min_order_size": "0.000001",
                "tick_size": "0.00000001",
                "neg_risk": false,
                "last_trade_price": "0",
            })),
        ),
    }
}

fn reject(st: &ClobState, msg: &str) -> (StatusCode, Json<Json_>) {
    st.rejected.fetch_add(1, Ordering::SeqCst);
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"success": false, "errorMsg": msg, "orderID": "", "status": ""})),
    )
}

async fn post_order(State(st): State<Arc<ClobState>>, Json(body): Json<Json_>) -> (StatusCode, Json<Json_>) {
    st.posts_received.fetch_add(1, Ordering::SeqCst);
    st.post_times.lock().unwrap().push(std::time::Instant::now());
    let latency = st.cfg.lock().unwrap().post_latency;
    if !latency.is_zero() {
        tokio::time::sleep(latency).await;
    }
    let failing = loop {
        let n = st.fail_posts.load(Ordering::SeqCst);
        if n == 0 {
            break false;
        }
        if st
            .fail_posts
            .compare_exchange(n, n - 1, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            break true;
        }
    };
    if failing {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(json!({"error": "Internal server error"})),
        );
    }
    let refusing = loop {
        let n = st.refuse_posts.load(Ordering::SeqCst);
        if n == 0 {
            break false;
        }
        if st
            .refuse_posts
            .compare_exchange(n, n - 1, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            break true;
        }
    };
    if refusing {
        st.rejected.fetch_add(1, Ordering::SeqCst);
        let status = if st.refuse_with_ok_status.load(Ordering::SeqCst) {
            StatusCode::OK
        } else {
            StatusCode::BAD_REQUEST
        };
        return (
            status,
            Json(json!({"success": false, "errorMsg": "refused by the stand in", "orderID": "", "status": ""})),
        );
    }
    let (order, sig) = match Order::from_wire(&body) {
        Ok(x) => x,
        Err(e) => return reject(&st, &e.to_string()),
    };
    let digest = match order.digest() {
        Ok(d) => d,
        Err(e) => return reject(&st, &e.to_string()),
    };
    // The signature must recover to the signer, and an EOA order (type 0) must be its own maker.
    match recover_address(&digest, &sig) {
        Ok(a) if a == order.signer => {}
        _ => return reject(&st, "invalid signature"),
    }
    if order.signature_type == 0 && order.maker != order.signer {
        return reject(&st, "maker and signer differ for an EOA order");
    }
    let id = format!("0x{}", hex_encode(&digest));
    let maker_hex = format!("0x{}", hex_encode(&order.maker));
    let mut orders = st.orders.lock().unwrap();
    let ok = |id: &str| {
        (
            StatusCode::OK,
            Json(
                json!({"success": true, "orderID": id, "status": "live", "makingAmount": "0", "takingAmount": "0", "errorMsg": ""}),
            ),
        )
    };
    if let Some(rec) = orders.get_mut(&id) {
        // The same signed order again (a resend after a crash): the same answer, nothing new.
        rec.received.posts += 1;
        return ok(&id);
    }
    // Stand-in rule, not from the documentation: a second order from one maker with a salt already used is
    // refused, so a sender that reused a nonce is caught.
    if !st.salts.lock().unwrap().insert((maker_hex.clone(), order.salt)) {
        drop(orders);
        return reject(&st, "salt already used by this maker");
    }
    let received = Received {
        id: id.clone(),
        maker: maker_hex,
        salt: order.salt,
        side: order.side,
        maker_amount: order.maker_amount,
        taker_amount: order.taker_amount,
        posts: 1,
    };
    orders.insert(
        id.clone(),
        Rec {
            order,
            received,
            polls: 0,
        },
    );
    st.order_seq.lock().unwrap().push(id.clone());
    ok(&id)
}

fn fmt_shares(base6: u64) -> String {
    let whole = base6 / 1_000_000;
    let frac = base6 % 1_000_000;
    if frac == 0 {
        whole.to_string()
    } else {
        let f = format!("{frac:06}");
        format!("{whole}.{}", f.trim_end_matches('0'))
    }
}

async fn get_order(State(st): State<Arc<ClobState>>, Path(id): Path<String>) -> (StatusCode, Json<Json_>) {
    st.polls.fetch_add(1, Ordering::SeqCst);
    let plan = st.cfg.lock().unwrap().fill_plan.clone();
    let mut orders = st.orders.lock().unwrap();
    let Some(rec) = orders.get_mut(&id) else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": "Order not found"})));
    };
    let pct = plan[(rec.polls as usize).min(plan.len() - 1)].min(100) as u64;
    rec.polls += 1;
    let size6 = rec.order.shares().0 / 100;
    let matched6 = size6 * pct / 100;
    (
        StatusCode::OK,
        Json(json!({
            "id": id,
            "status": if matched6 == size6 { "MATCHED" } else { "LIVE" },
            "owner": "stand-in-owner",
            "maker_address": format!("0x{}", hex_encode(&rec.order.maker)),
            "market": "0x0000000000000000000000000000000000000000000000000000000000000001",
            "asset_id": rec.order.token_id,
            "side": side_str(rec.order.side),
            "original_size": fmt_shares(size6),
            "size_matched": fmt_shares(matched6),
            "price": rec.order.implied_price().to_string(),
            "outcome": "YES",
            "expiration": rec.order.expiration.to_string(),
            "order_type": "FAK",
            "associate_trades": [],
            "created_at": rec.order.timestamp_ms / 1000,
        })),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(s: &str) -> Fixed {
        Fixed::parse(s).unwrap()
    }

    fn sample(wallet: &Wallet) -> Order {
        let a = wallet.address();
        Order {
            salt: 1,
            maker: a,
            signer: a,
            token_id: "71321045679252212594626385532706912750332728571942532289631379312455583992563".into(),
            maker_amount: 5_200_000,
            taker_amount: 10_000_000,
            side: OrderSide::Buy,
            signature_type: 0,
            timestamp_ms: 1_700_000_000_000,
            metadata: [0; 32],
            builder: [0; 32],
            expiration: 0,
        }
    }

    #[test]
    fn a_buy_gets_collateral_as_maker_and_shares_as_taker() {
        // 10 shares at 0.52 costs 5.2 in collateral.
        assert_eq!(
            amounts(OrderSide::Buy, f("10"), f("0.52")).unwrap(),
            (5_200_000, 10_000_000)
        );
    }

    #[test]
    fn a_sell_gets_shares_as_maker_and_collateral_as_taker() {
        assert_eq!(
            amounts(OrderSide::Sell, f("10"), f("0.52")).unwrap(),
            (10_000_000, 5_200_000)
        );
    }

    #[test]
    fn rounding_keeps_the_implied_price_inside_the_limit() {
        // 3 shares at 0.333333: the exact cost is 0.999999.
        let (m, t) = amounts(OrderSide::Buy, f("3"), f("0.333333")).unwrap();
        assert_eq!((m, t), (999_999, 3_000_000));
        let (m, t) = amounts(OrderSide::Sell, f("0.000007"), f("0.5")).unwrap();
        // 7 base units of shares at 0.5 want 3.5, rounded up to 4 so the price is not below the limit.
        assert_eq!((m, t), (7, 4));
        let o = Order {
            maker_amount: m,
            taker_amount: t,
            side: OrderSide::Sell,
            ..sample(&Wallet::throwaway().0)
        };
        assert!(o.implied_price() >= f("0.5"));
    }

    #[test]
    fn an_amount_that_rounds_to_nothing_is_refused() {
        assert!(amounts(OrderSide::Buy, f("0.0000001"), f("0.5")).is_err());
        assert!(amounts(OrderSide::Buy, f("1"), Fixed::ZERO).is_err());
        assert!(amounts(OrderSide::Buy, f("0.000001"), f("0.0001")).is_err());
    }

    #[test]
    fn the_signed_order_types_list_the_documented_fields_in_order() {
        let names: Vec<&str> = ORDER_TYPES.iter().map(|(n, _)| *n).collect();
        assert_eq!(
            names.join(", "),
            "salt, maker, signer, tokenId, makerAmount, takerAmount, side, signatureType, timestamp, metadata, builder"
        );
        let w = Wallet::throwaway().0;
        let td = sample(&w).typed_data().unwrap();
        assert_eq!(
            td.encode_type("Order").unwrap(),
            "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)"
        );
    }

    #[test]
    fn a_signed_order_recovers_to_its_signer_and_changing_a_field_breaks_it() {
        let (w, _) = Wallet::throwaway();
        let o = sample(&w);
        let sig = o.sign(&w).unwrap();
        assert_eq!(recover_address(&o.digest().unwrap(), &sig).unwrap(), w.address());
        for changed in [
            Order { salt: 2, ..o.clone() },
            Order {
                maker_amount: o.maker_amount + 1,
                ..o.clone()
            },
            Order {
                taker_amount: o.taker_amount + 1,
                ..o.clone()
            },
            Order {
                side: OrderSide::Sell,
                ..o.clone()
            },
            Order {
                timestamp_ms: o.timestamp_ms + 1,
                ..o.clone()
            },
            Order {
                token_id: "5".into(),
                ..o.clone()
            },
            Order {
                signature_type: 1,
                ..o.clone()
            },
        ] {
            let r = recover_address(&changed.digest().unwrap(), &sig).unwrap();
            assert_ne!(r, w.address());
        }
    }

    #[test]
    fn expiration_is_on_the_wire_but_not_in_the_signature() {
        let (w, _) = Wallet::throwaway();
        let o = sample(&w);
        let o2 = Order {
            expiration: 99,
            ..o.clone()
        };
        assert_eq!(o.digest().unwrap(), o2.digest().unwrap());
        assert_ne!(o.to_wire(&o.sign(&w).unwrap()), o2.to_wire(&o.sign(&w).unwrap()));
    }

    #[test]
    fn the_wire_form_round_trips_and_has_the_documented_keys() {
        let (w, _) = Wallet::throwaway();
        let o = sample(&w);
        let sig = o.sign(&w).unwrap();
        let wire = o.to_wire(&sig);
        let keys: Vec<&str> = wire["order"].as_object().unwrap().keys().map(|k| k.as_str()).collect();
        for k in [
            "maker",
            "signer",
            "tokenId",
            "makerAmount",
            "takerAmount",
            "side",
            "expiration",
            "timestamp",
            "metadata",
            "builder",
            "signature",
            "salt",
            "signatureType",
        ] {
            assert!(keys.contains(&k), "{k} is on the wire");
        }
        let (back, sig2) = Order::from_wire(&wire).unwrap();
        assert_eq!(back, o);
        assert_eq!(sig2, sig.to_vec());
    }

    #[test]
    fn a_wire_body_with_a_bad_field_is_refused() {
        let (w, _) = Wallet::throwaway();
        let o = sample(&w);
        let mut wire = o.to_wire(&o.sign(&w).unwrap());
        wire["order"]["side"] = json!("HOLD");
        assert!(Order::from_wire(&wire).is_err());
        let mut wire = o.to_wire(&o.sign(&w).unwrap());
        wire["order"]["makerAmount"] = json!("x");
        assert!(Order::from_wire(&wire).is_err());
        assert!(Order::from_wire(&json!({})).is_err());
    }

    #[test]
    fn shares_and_implied_price_read_back_from_the_amounts() {
        let (w, _) = Wallet::throwaway();
        let o = sample(&w);
        assert_eq!(o.shares(), f("10"));
        assert_eq!(o.implied_price(), f("0.52"));
        let s = Order {
            side: OrderSide::Sell,
            maker_amount: 10_000_000,
            taker_amount: 5_200_000,
            ..o
        };
        assert_eq!(s.shares(), f("10"));
        assert_eq!(s.implied_price(), f("0.52"));
    }

    #[test]
    fn shares_print_without_trailing_zeros() {
        assert_eq!(fmt_shares(10_000_000), "10");
        assert_eq!(fmt_shares(10_500_000), "10.5");
        assert_eq!(fmt_shares(1), "0.000001");
        assert_eq!(fmt_shares(0), "0");
    }

    #[test]
    fn a_chunked_body_is_joined() {
        assert_eq!(dechunk("5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"), "hello world");
    }

    #[test]
    fn a_chunked_body_stops_at_the_zero_chunk_and_at_a_truncated_one() {
        assert_eq!(
            dechunk("3\r\nabc\r\n0\r\n\r\n4\r\nmore\r\n"),
            "abc",
            "nothing after the zero chunk counts"
        );
        assert_eq!(
            dechunk("5\r\nhello"),
            "hello",
            "a last chunk with no trailing CRLF is whole"
        );
        assert_eq!(
            dechunk("9\r\nhello"),
            "",
            "a chunk shorter than it says is dropped, not sliced"
        );
        assert_eq!(dechunk("zz\r\nabc"), "");
    }

    #[test]
    fn the_order_errors_say_what_was_wrong() {
        let a = amounts(OrderSide::Buy, f("0.0000001"), f("0.5")).unwrap_err();
        assert!(a.to_string().contains("rounds to nothing"), "{a}");
        let z = amounts(OrderSide::Buy, f("1"), Fixed::ZERO).unwrap_err();
        assert!(z.to_string().contains("rounds to nothing"), "{z}");
        let c = amounts(OrderSide::Buy, f("0.000001"), f("0.0001")).unwrap_err();
        assert!(c.to_string().contains("costs nothing"), "{c}");
        assert_eq!(OrderError::Wire("x".into()).to_string(), "bad order: x");
        assert_eq!(
            OrderError::Sign(Eip712Error::UnknownType("T".into())).to_string(),
            "unknown type T"
        );
    }
}
