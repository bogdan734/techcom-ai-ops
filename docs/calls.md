# Attaching a voice provider

Phone calls are a delivery channel with the same guarantees as messages: one idempotency key per
call, a lookup before any redial, and an outcome that is recorded once. Any telephony backend
that speaks the small contract below can be attached with settings alone.

```
CALLS=true
CALLS_PROVIDER=http
CALLS_API_URL=https://voice.example.com        # https, or http only for this computer
CALLS_API_KEY=...                              # sent as  Authorization: Bearer ...
CALL_PURPOSE=reminder                          # or verification: the call asks the questions
CALLS_CALLBACK_URL=https://desk.example.com/api/webhooks/call
CALLS_WEBHOOK_SECRET=at-least-16-characters
```

## What the service sends

`POST {CALLS_API_URL}/calls` with the header `Idempotency-Key: <32 hex characters>` and a JSON body:

| Field | When | Meaning |
|---|---|---|
| `to` | always | the person's phone number, `+15551234567` |
| `task_id`, `decision` | always | what the call is about; `decision` is `notify` |
| `body` | always | the words to say; with `verification` it already contains the questions |
| `purpose` | always | `reminder` (say it) or `verification` (ask, and listen) |
| `questions` | verification | `[{"number": 1, "prompt": "..."}]`, the scored questions in order |
| `reply_to` | when `CALLS_CALLBACK_URL` is set | where to report back |

## What it has to answer

- `201` for a new call, or `200` with the stored record for a key it already knows. The same key
  never rings twice. The record carries `message_id` (the provider's call id) and
  `answered_by`: `person`, `machine`, `none`, or `pending` while the outcome is not known.
- `GET {CALLS_API_URL}/calls/{key}` returns that record, or `404` for a call it never placed.
  The service uses it after a lost response, and on every run for calls still `pending`.

A machine counts as delivered for a reminder and not for a verification call; nobody reaching
a person tells the issuer.

## Reporting back

For a provider that finishes calls later, or that takes the answers by voice, the app exposes
`POST /api/webhooks/call` with the header `X-Webhook-Secret: <CALLS_WEBHOOK_SECRET>`:

```json
{"call_key": "<the Idempotency-Key>", "answered_by": "person", "answers": {"1": "…", "2": "…"}}
```

`answered_by` settles a pending call once; `answers` (verification calls only, and only from a
call a person answered) are recorded as that person's answers, exactly as if they had typed
them, and the next run grades them. The route is off without a secret, it needs the app to be
reachable from the provider (a tunnel or a reverse proxy), and answers are accepted once.

## Speech to text

The service does not transcribe. A verification call needs a voice bot between the telephony
backend and this contract. It has to:

1. take the service's `POST /calls` and start a call, keeping the idempotency key against the
   provider's call id;
2. read the `questions` aloud and transcribe each spoken answer;
3. post the outcome and the transcripts to `reply_to` as shown above;
4. answer `GET /calls/{key}` from what it stored.

That bridge is not part of this repository and has not been run against a live provider; the
mock in `src/followup/mock_api.py` is the reference for the contract.
