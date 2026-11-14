# An API on Modal, alerts through Amazon SES and a Netlify status function

Three pieces were added to ledgerbell, each run for real and each tested offline.

| Piece | Where | What it does |
|---|---|---|
| FastAPI app | `src/ledgerbell/api.py`, `serverless/modal_app.py` | `GET /health`, `POST /risk/check` (one proposed order and an account snapshot in, the risk verdict from `risk.check` out) and `POST /alerts` (a subject and body in, sent through the alert channels). Deployed on Modal |
| SES alert channel | `src/ledgerbell/ses.py`, `alerts.py` | Sends an alert through the SES v2 `SendEmail` call with boto3, next to the email, webhook and Twilio channels |
| Netlify function | `netlify/functions/status.mjs`, `netlify/lib/evaluate.mjs`, `src/ledgerbell/heartbeat.py` | `GET /status` answers 200 while the bot is running and 503 when it has been silent for more than 36 weekday hours, for an uptime monitor |

## The API

The risk endpoint holds no state and calls no broker. It is the risk rules behind a JSON door, so a remote caller can ask whether an order would pass and has no way to place one. Every route except `/health` needs an `X-Api-Key` header, compared with `hmac.compare_digest`; an app built with an empty key refuses to start, so it can never run open. The alert route takes no recipient from the request (a request body with a `to` field is a 422), so it cannot be used to mail anyone else. The interactive docs pages are off.

`serverless/modal_app.py` wraps `app_from_env()` as a Modal web endpoint. Every setting comes from a Modal Secret named `ledgerbell-api`, so nothing secret is in the image or the repository. That Secret holds a dedicated IAM key that may only call `ses:SendEmail` on one verified address, not the key of the account that made it.

```
python -m pip install modal
python -m modal token new
python -m modal secret create ledgerbell-api LEDGERBELL_API_KEY=... LEDGERBELL_SES_TO=... LEDGERBELL_SES_FROM=... LEDGERBELL_SES_REGION=ap-southeast-2 AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
python -m modal deploy serverless/modal_app.py
LEDGERBELL_API_KEY=... python scripts/modal_check.py https://<workspace>--ledgerbell-api-web.modal.run --out docs/modal-run-captured.txt
```

### What ran for real

`scripts/modal_check.py` against the deployed endpoint on Modal's free plan ([captured output](modal-run-captured.txt)). The call goes from the Philippines to Modal's us-east region, so most of each time below is distance.

| Call | Result |
|---|---|
| First `GET /health` after the deploy (a cold start) | 200 in 4648 ms |
| Five warm `GET /health` calls | median 1069 ms, range 1001 to 1130 ms |
| `GET /docs` | 404 |
| `POST /risk/check` with no key, and with a wrong key | 401 and 401 |
| 10 SPY at 500 | allowed, stop at 485.0 |
| 100 SPY at 500 | cut to 10 by the 5 percent size limit |
| GME | refused, not on the watchlist |
| `POST /alerts` | `{"sent": true, "problems": []}` in 2743 ms, one mail delivered through SES |

## SES

`ses.send` builds the SES v2 request and boto3 signs it with the usual AWS credential chain, in a fresh session each call so a rotated key is picked up. A subject is cut to one line of at most 200 characters, because a line break in a subject is a header injection. A failure is reported with SES's own error code and message (for example `MessageRejected: Email address is not verified`) and never raised into a routine. The settings are `ses_to`, `ses_from`, `ses_region` and, for tests, `ses_endpoint_url`; no key is a setting.

A new SES account is in the sandbox, which `scripts/ses_check.py` reports: sending enabled, production access off, 200 mails per 24 hours and 1 per second. The sender and the recipient must both be verified identities. The script asked SES to send the verification mail for `danielalexiscruz.pro@gmail.com`, the link in that mail was opened, and one alert was sent to the same address ([captured output](ses-run-captured.txt)). The script reads the credentials file only through boto3, never prints a key and refuses to write a transcript that contains the access key id.

## The Netlify function

`ledgerbell heartbeat` writes `status.json` from the ledger: the time of the last event, the routine, whether the kill switch is engaged and the mode. It holds no account figure, no symbol and no order, so it is safe to publish. The function reads that file from the site's own static files and `evaluate.mjs` judges it. The bot runs on weekdays only, so Saturday and Sunday do not count towards the 36 hours: Friday evening to Monday morning is normal and a missed Monday and Tuesday is not. An engaged kill switch answers 200 with `state: killed`, because the bot did its job; a missing file, junk, a missing time, a time in the future and a file that was not refreshed all answer 503.

It is deployed at https://ledgerbell-status.netlify.app/status (Netlify's free plan, no card). The file it serves was written by `ledgerbell heartbeat` from the ledger of one `eod` routine run against the local test server on made-up prices, so it says `mode: standin`. The function answered 200 with `state: ok` at deploy time and takes about 0.7 to 0.9 s from the Philippines.

## Tests

* `tests/test_alerts_ses.py` (15 tests) drives boto3 over real HTTP against a local SES v2 stand-in written from the documentation. It checks the SigV4 credential scope (key, region, service), the request body, a subject with a line break, a recipient or sender that is not verified (400 `MessageRejected`), throttling, a wrong access key (403), a dead port, a missing boto3, and that the secret key is in no request, no problem string and no setting.
* `tests/test_api.py` (19 tests) uses FastAPI's test client: each risk verdict against the rules, the key on every protected route, an empty key refused at build time, bad input as a 422 and never a 500, an alert that cannot name a recipient, and the app built from the environment the way Modal starts it.
* `tests/test_heartbeat.py` (5 tests) checks what the file holds and that it never holds an account figure, and that `ledgerbell heartbeat` needs no broker keys.
* `node --test netlify/test/evaluate.test.mjs` (11 tests) covers the weekend rule, a killed bot, a stale file, junk and a time in the future, plus the function itself with `fetch` replaced.
* `python scripts/mutants.py` has 15 more breaks for these pieces: the key check, the empty key, the docs pages, the kill flag, the recipient rule, the subject rule, the SES error code and the heartbeat fields. The first run of them left two alive: a test that would have passed with the address check removed, and a break of the docs pages that changed nothing (FastAPI serves no docs page when the schema address is off). The test now checks the message, the break now turns the schema address on, and the second run is in [mutation-run-serverless-captured.txt](mutation-run-serverless-captured.txt). The 86 older breaks were not run again for this change.

## Limits

* The SES account is in the sandbox, so alerts reach only verified addresses. Production access was not requested.
* The Modal endpoint scales to zero and costs a cold start of several seconds after a quiet spell. It has no rate limit of its own.
* The status page serves a heartbeat file that a person publishes with `ledgerbell heartbeat` and a Netlify deploy. Nothing publishes it on a schedule yet, so after 36 weekday hours the page answers 503 until someone publishes a new file.
* The API is stateless. It does not read the ledger or the kill file, so the caller passes `kill_engaged` and the account snapshot.
* The Modal, SES and Netlify runs were each done once, on 2026-11-14, from one machine.
