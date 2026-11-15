# CLOB and EIP-712 contract notes

*The parts of Polymarket's public documentation and of the EIP-712 text that the order code and the CLOB stand in were written from, with the page each came from. Every page was loaded in Chromium on 2026-11-15 (`webread.cjs`, HTTP 200). Nothing here was observed on the live service: no request was ever sent to Polymarket.*

## Sources

* Polymarket Protocol V2 API Migration: https://docs.polymarket.com/migrate/polymarket-v2/api-integrations
* Builder Fees: https://docs.polymarket.com/programs/builders/fees
* Place Orders: https://docs.polymarket.com/trading/place-orders
* Prices and Order Books: https://docs.polymarket.com/market-data/prices-order-books
* The CLOB OpenAPI file: https://docs.polymarket.com/api-spec/clob-openapi.yaml
* EIP-712: https://eips.ethereum.org/EIPS/eip-712
* The EIP's Example.js: https://raw.githubusercontent.com/ethereum/EIPs/master/assets/eip-712/Example.js

## Polymarket, and what the code does with it

The domain, from the "Sign and Submit the V2 Order" step of the API migration page:

```
{
  "name": "Polymarket CTF Exchange",
  "version": "3",
  "chainId": 137,
  "verifyingContract": "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"
}
```

`clob.rs` uses these four values as they are. The same page says: "Keep the CTFExchangeV2 signed order structure, set order.tokenId to the V2 position ID, and re-sign."

The signed order's fields, from "Signed Order Struct" on the Builder Fees page:

```
salt, maker, signer, tokenId, makerAmount, takerAmount,
side, signatureType, timestamp, metadata, builder
```

`ORDER_TYPES` in `clob.rs` lists exactly these, in this order. **The pages name the fields and their order but not their Solidity types.** The types in `ORDER_TYPES` are this crate's reading: `uint256` for `salt`, `tokenId`, the two amounts and `timestamp`, `address` for `maker` and `signer`, `uint8` for `side` and `signatureType`, `bytes32` for `metadata` and `builder` (the Builder Fees page says the builder code is "a bytes32"). `side` is encoded 0 for BUY and 1 for SELL, which no page I read states. So a signature made by this crate is not claimed to be one the real exchange would accept. What the tests prove is that the hashing and signing follow the EIP-712 text (see below) and that the stand in CLOB recovers the signer from the same rules.

There is no nonce in that list. The OpenAPI schema for the order says the same about `expiration`: "In CLOB V2, `expiration` remains in the POST /order wire body for GTD/order-expiry handling, but it is not part of the EIP-712 signed order struct." The worker therefore keeps its own monotonic per wallet counter in SQLite and uses it as the order's `salt`, so two orders from one wallet never share one. That is this crate's design, not something the documentation describes.

The wire body of `POST /order`, from the OpenAPI schema `Order` and `SendOrder` (`Order::to_wire`):

```
order: maker, signer, tokenId, makerAmount, takerAmount, side ("BUY" or "SELL"), expiration,
       timestamp, metadata, builder, signature, salt (integer), signatureType (0 to 3)
owner, orderType (GTC, FOK, GTD, FAK), deferExec, postOnly
```

The amounts, from the same schema: "Amount the maker is providing in fixed-math with 6 decimals". `clob::amounts` follows that: shares and collateral in 6 decimals, a buy's `makerAmount` is collateral and its `takerAmount` is shares, a sell the other way round. The rounding (shares down, a buy's collateral down, a sell's collateral up, so the implied price is never worse than the limit) is this crate's rule. The Place Orders page says the SDK "rounds BUY shares down to supported precision without increasing the signed spend".

The response of `POST /order`, from `SendOrderResponse`: `success`, `orderID` ("Unique identifier for the order (order hash)"), `status` (`live`, `matched` or `delayed`), `makingAmount`, `takingAmount`, `errorMsg`. The stand in answers `live` and uses the EIP-712 digest as the id; the documentation does not say how Polymarket computes its order hash.

The book, from `GET /book?token_id=` and the `OrderBookSummary` schema: `market`, `asset_id`, `timestamp`, `hash`, `bids`, `asks` (each `{price, size}` as strings), `min_order_size`, `tick_size`, `neg_risk`, `last_trade_price`. **The pages disagree on order:** the OpenAPI file says bids are sorted by price descending and asks ascending, while the Prices and Order Books page says "Bids are ordered by ascending price and asks by descending price, so the best bid and ask are the last entries". The stand in follows the OpenAPI file, and the worker does not depend on either, because it builds its book from the levels and finds the best price itself.

The order's state, from `GET /data/order/{orderID}`: `status` (`LIVE`, `MATCHED`, ...), `original_size`, `size_matched`, `price`. The worker reads `size_matched`, and counts a fill only after it has stayed unchanged for a window (`FillStabilizer`). The stand in changes `size_matched` between polls on a script the test sets.

Two rules in the stand in are not from the documentation: a second order from one maker with a salt that maker already used is refused, and the same signed order posted twice (same hash) gets the same answer and is kept once. Both exist so the tests can tell a sender that reused a nonce from one that resent the same order after a crash.

The stand in does not check API keys (the real routes need `POLY_API_KEY`, `POLY_SIGNATURE` and the other headers), balances, allowances, tick size, minimum size, matching or fees. The Data API client in the job post's stack is not built.

## EIP-712, and what the code does with it

From the EIP text: `encode(domainSeparator : 𝔹²⁵⁶, message : 𝕊) = "\x19\x01" ‖ domainSeparator ‖ hashStruct(message)`, `hashStruct(s : 𝕊) = keccak256(typeHash ‖ encodeData(s)) where typeHash = keccak256(encodeType(typeOf(s)))`, and `domainSeparator = hashStruct(eip712Domain)`. `eip712.rs` implements these for `uintN`, `address`, `bool`, `bytesN`, `bytes`, `string` and nested structs. Arrays are refused.

The test vector is the Mail example from the EIP and its Example.js, which states these values and which `eip712.rs` reproduces in its tests:

```
encodeType            Mail(Person from,Person to,string contents)Person(string name,address wallet)
typeHash              0xa0cedeb2dc280ba39b857546d74f5549c3a1d7bdc2dd96bf881f76108e23dac2
hashStruct(message)   0xc52c0ee5d84264471806290a3f2c4cecfc5490626bf912d01f240d7a274b371e
domainSeparator       0xf2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f
signHash              0xbe609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2
private key           keccak256("cow")
address               0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826
v, r, s               28, 0x4355c47d63924e8a72e509b65029052eb6c299d53a04e167c5775fd466751c9d,
                      0x07299936d304c153f6443dfa05f40ff007d72911b6f72307f996231605b91562
```

The EIP's `eth_signTypedData` example gives the same signature as one 65 byte string ending in `1c`. The key in that vector is public and exists only to reproduce it; every other key in this crate's tests, demo and benchmarks is a random throwaway made when the process starts.
