# Changelog

## Unreleased

Everything below is opt-in. With no org file and no new setting, a run produces the same
`decisions.json`, delivery log and audit events as the submitted version (`b67e0c6`); a test and
`make demo` pin that, and a `state.db` written by the submitted version keeps suppressing what it
already sent.

### Hardening
- `followup run --dry-run`: decide and draft over an in-memory copy of the state.
- Dead letters: `MAX_FAILED_RUNS`, `followup list dead`, `followup requeue`.
- Approval gate for escalations: `APPROVE_ESCALATIONS`, `followup approve`.
- Reminder cadence ending in one escalation: `REMIND_EVERY_DAYS`, `MAX_REMINDERS`.
- Schema versioning through `PRAGMA user_version`.

### Team layer (`ORG_FILE`)
- One SQLite file per department with an application id, its own name and a checksummed
  migration ledger; `followup team db status | verify | migrate | split`; roles enforced by
  SQLite's authorizer.
- A run lock, so two runs on one directory cannot overlap, and a run ledger that tells a replay
  from a run that disagrees with itself.
- Personal accounts for the app (scrypt, lockout, HttpOnly sessions); issuers give tasks, assignees
  accept, hand in and are sent back; a brief and a list of gaps for every spec.
- Completion check: a teach-back quiz from the spec, graded by key terms with the model as a
  second opinion and an audit sample; a code check for the names a spec asks for.
- Check levels from a result ledger (decayed Wilson bound, acceptance-sampling levels), hidden
  by default or shown to issuers only.
- A record for the model itself: grounding, a ledger per feature, a labelled set and Cohen's kappa
  (`followup team calibrate`), and a fall back to rules when the record turns bad.
- Notices with ids from what they are about; chat with a quote-or-abstain assistant and an SLA
  that climbs the reporting line; support tickets; conversations that can be marked done.
- Phone calls as a channel behind reminders, with consent and a calling window, a real provider
  over HTTP, a webhook, and a verification mode ([docs/calls.md](docs/calls.md)).
- The app (`followup team serve`) in English, Russian and Ukrainian.
