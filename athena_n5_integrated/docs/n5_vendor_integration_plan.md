# ATHENA N5 — Vendor Integration Plan (distributors, facility data, EMS / ODM partners)

All live integrations are feature-flagged (`N5_ENABLE_LIVE_APIS=false` by default). In the tests they
run only against mocked endpoints. Each needs an N0 `SourceRecord` that permits `SOURCING`; revoking
it there blocks all N5 use of that data at once. Defaults:

| Connector | N0 source |
|---|---|
| DigiKey | `SRC-DIGIKEY` |
| Mouser | `SRC-MOUSER` |
| Nexar | `SRC-NEXAR` |
| Open Supply Hub | `SRC-OSH` |

Drop-zone and manual quotes name their own source.

## 1. Common licensing posture (enforced in `app/n5/connectors/base.py` and `quotes.py`)

| Control | Implementation |
|---|---|
| On demand, project scoped | `lookup` only accepts lines of an **active** BOM intake of the caller's tenant. There is one MPN per upstream call and no search, crawl or bulk endpoint. Lookups for anything else give `LOOKUP_OUT_OF_SCOPE`. |
| No bulk download / DB building | Upstream payloads are parsed in memory. Only the fields used for this project's decision (price breaks, MOQ, pack, stock, SKU) are kept, with the response SHA-256, as `PROJECT_DECISION_RECORD`. |
| TTL cache | In memory, keyed (tenant, project, connector, MPN), TTL = min(`N5_CACHE_TTL_S`, the connector cap). It never touches disk. |
| Per-line budget | At most `max_lookups_per_line` upstream calls per connector, line and TTL window. |
| Rate limits | A token bucket (calls / minute) and a rolling 24 h quota per connector. Both refuse with `RATE_LIMITED` rather than queue. |
| No training / redistribution | Records carry `training_allowed: false` and `redistribution: INTERNAL_PROJECT_ONLY`. Payloads never enter a corpus or training set. |
| Provenance log | `n5_connector_calls` records connector, operation, key, intake, line, requester, time, cache hit, HTTP status, response hash and outcome (`OK / NOT_FOUND / ERROR / DISABLED / RATE_LIMITED / OUT_OF_SCOPE`). |
| Secrets | Read from settings (environment / secret store) and redacted from errors. They are never stored in records, logs or audit events. Mouser's query-string key is masked. |

**Before production**, legal review must confirm each agreement's allowance for the decision-record
retention and the cache TTL (backlog N5-B02).

## 2. DigiKey (OAuth2, Product Information API v4)

- **Token:** `POST {base}/v1/oauth2/token` with `grant_type=client_credentials`, cached until expiry.
- **Lookup:** `GET {base}/products/v4/search/{productNumber}/productdetails` with headers
  `X-DIGIKEY-Client-Id`, `Authorization: Bearer`, `X-DIGIKEY-Locale-Site` and `-Currency`.
- **Mapping:** each `ProductVariations[]` becomes one offer.
  - `StandardPricing` gives the price breaks; JSON numbers are parsed as `Decimal`.
  - `MinimumOrderQuantity` gives the MOQ.
  - Tape & Reel: `StandardPackage` gives the pack multiple.
  - Digi-Reel: `DigiReelFee` becomes a `REELING` line charge.
  - `QuantityAvailableforPackageType` gives the stock.
- **Dropped:** variations with `MarketPlace: true` (third-party sellers).

## 3. Mouser (API key, Search API v1)

- **Request:** `POST {base}/api/v1/search/partnumber?apiKey=…` with
  `{"SearchByPartRequest": {"mouserPartNumber": MPN, "partSearchOptions": "Exact"}}`.
- **Matching:** only parts whose `ManufacturerPartNumber` equals the MPN exactly are kept.
- **Parsing:** price strings such as `₹6.50`, `$0.10` or `1.234,56 €` are parsed exactly. `Min` gives
  the MOQ, `Mult` the pack multiple and `AvailabilityInStock` the stock.

## 4. Nexar / Octopart (OAuth2 + GraphQL, scope `supply.domain`)

- **Query:** `supSearchMpn(q: MPN, limit: 3)`, with `sellers(authorizedOnly: true)` and
  `offers { sku inventoryLevel moq orderMultiple factoryLeadDays prices }`.
- **Sellers:** each seller becomes its own quote, mapped to an N5 supplier record by normalised
  legal name. An unmatched seller becomes `UNREGISTERED-<name>`, which the guardrails reject
  (`SUPPLIER_NOT_APPROVED`).
- Nexar's `isAuthorized` flag is evidence for supplier quality, never an approval.

## 5. Open Supply Hub (REST or CSV)

- **Live:** `GET {base}/api/facilities/{os_id}/` with `Authorization: Token …`, returning a GeoJSON
  Feature.
- **Offline:** an OSH CSV export (`os_id, name, address, country_code`) uploaded as `osh_csv`.
- **Result:** a site is `osh_verified` only when the OS ID resolves and the country matches. The
  evidence (name, address, method, hash, verifier) is stored on the site, with attribution as OSH
  requires.

## 6. EMS / ODM signed drop zone

The partner sends an `athena-ems-quote/1` manifest plus the files it lists
(`POST /v1/n5/quotes/ems`):

| Item | Requirement |
|---|---|
| Quotation document | a PDF, CSV (`line, quoted_mpn, manufacturer, currency, moq, pack_multiple, availability, stock_qty, lead_time_days, price_breaks` with `qty:price;qty:price`) or X12 843 interchange (`application/edi-x12`, `.edi` / `.x12`). Its SHA-256 is in the manifest. |
| Signature | a detached Ed25519, RSA-PSS-SHA256 or RSA-PKCS1v1.5-SHA256 signature over the document bytes, made with a key registered on the supplier record (`signing_keys`) |
| Capacity allocation | allocated quantity, period, site and the SHA-256 of an attached signed allocation letter. It must cover the build quantity (EMS) or Q_order (factory order) through today + lead time. |
| NRE, service terms | recorded and passed to N7 as excluded items; never in the material total |
| Attachments | each listed with path, SHA-256 and role; undeclared files are rejected |

**The X12 843 profile** is a documented subset (see `ems_dropzone.parse_x12_843`):

- header segments `BQT` (quote reference), `CUR` and `DTM*036` (valid until);
- one `PO1` per line, with MPN qualifier `MG` and supplier SKU `VP`;
- `CTP**PRP` price breaks, `PO4` pack multiple, `QTY*MO` MOQ, `QTY*33` stock and `LDT` lead time.

Full EDI translation (envelopes, acknowledgements, 997) is out of scope (backlog N5-B05).

## 7. Onboarding checklist per vendor

1. Register the N0 source with its licence reference. Permit `SOURCING` only; never
   `MODEL_TRAINING`.
2. A rights approver who is not the registrant approves the source.
3. Register the N5 supplier record and verify its channel proofs or OSH sites. A `supplier_quality`
   reviewer who is not the submitter approves it, with an expiry date.
4. For live APIs: store the credentials in the secret store, enable the flag in the target
   environment, and set the rate limits and quota to the contract.
5. For EMS partners: register the signing public key, then agree the manifest and the CSV / X12
   profile.
