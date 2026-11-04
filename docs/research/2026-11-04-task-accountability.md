# Research: checking that a task was understood and done

Date: 2026-11-04

## Question and constraints

How does a follow-up service check that an assignee understood and finished a task, spend less checking effort on people with a good record, and stop an LLM's mistakes from becoming verdicts about people?

Constraints: a run with the old settings gives the old output. The code uses the standard library and the dependencies already in the repository. LLM output stays untrusted until a rule has checked it. The whole flow runs offline against a stub. A second run sends nothing.

## Options

| Area | What exists | What this repository takes | Source |
|---|---|---|---|
| Confirm understanding | Teach-back: ask the person to explain, in their own words, what they need to know or do | The completion quiz is a teach-back, and its questions come from the task text | AHRQ TeamSTEPPS |
| Checking effort by record | Acceptance sampling moves a supplier between normal, tightened and reduced inspection by recent results. The 2026 edition of ISO 2859-1 adds skip-lot sampling to the switching rules | Three check levels per person and a spot check for people on the lightest one. The thresholds are my own and live in configuration | ISO 2859-1 |
| Score from few results | The lower bound of the Wilson interval gives a small sample a low score and a large clean sample a high one. A critic calls this guilty until proven innocent | Taken, with a probation count so a new person gets the standard plan and not the strictest | Miller; LessWrong post |
| Grounding model output | Ask for word-for-word quotes first, find a supporting quote for each claim and retract the claim when none exists, run the prompt several times and compare. The guidance says this reduces hallucination and does not remove it | Every question, answer key and assistant reply carries a quote that has to appear in the task text, or the item is dropped | Anthropic docs |
| Judge bias | LLM judges show position, verbosity and self-preference bias. A 2026 study finds that fixing one bias can worsen another | Key terms from the task text decide first. The model grades second, and a second model audits a sample of its grades | arXiv 2411.15594, 2410.21819, 2604.23178 |
| Judge drift | Practitioner guides compare the judge with a labelled set (Cohen's kappa, true positive and true negative rates) and repeat the comparison when the judge or the data changes | `team calibrate` scores the grader on a labelled set stored in the repository. Below a floor, grading falls back to rules | oneuptime, Galileo (secondary) |
| Data separation | One SQLite file per tenant gives physical isolation and simple backup and deletion. Cross-tenant queries get harder and each file needs its own migration history. The authorizer callback can deny ATTACH and table reads | One file per department, a header id and a checksummed migration ledger in each, and an authorizer per role | Turso; Python docs |
| Escalation | PagerDuty policies notify one level, wait for a timeout, go up a level and stop at acknowledgement | An unanswered chat question climbs the reporting line on the same rule | PagerDuty docs |
| Duplicate protection | The Idempotency-Key header (IETF draft 07, October 2025) and the outbox pattern. Both are in the service already | A run lock and an input digest ledger close the two-process gap | IETF |
| Calls | Voice providers report who answered: person, machine, fax or unknown, in a blocking or an asynchronous mode | A call channel with a calling window, a consent flag and a person-or-machine result. No speech-to-text | Twilio docs |
| Law and fairness | The EU AI Act lists AI that allocates tasks or monitors and evaluates workers as high risk, and reports put the obligations at 2 December 2027 after the Digital Omnibus. Illinois HB 3773 took effect on 1 January 2026, bars discriminatory AI in employment decisions and requires notice | Trust sets how much we check and does not feed pay or discipline. Staff can see that the system exists, and `trust explain` lists the events behind a level. This is not legal advice | Ogletree; Hunton |

## Recommendation

Build it in the repository. The techniques exist as ideas, and no library covers this combination. Take acceptance-sampling levels, a Wilson-style score, quote grounding, a calibrated judge and one database per department, and write them on the standard library so the demo runs offline.

## Improvements worth trying

- Speech-to-text, so a call can take the answer.
- Item analysis: retire questions that everyone passes or everyone fails.
- Postgres schemas with row-level security once one writer per file is too few.
- A signed audit log for trust events.
- A grader from a different model family as the auditor.

## Open questions

- Should people see their own level, given the notice duties above?
- Who owns an appeal when a person disputes a level?

## Sources

Accessed 2026-11-04. P is a primary source and S is secondary.

- P: [AHRQ TeamSTEPPS, Teach-Back](https://www.ahrq.gov/teamstepps-program/curriculum/communication/tools/teachback.html)
- P: [ISO 2859-1:2026 preview](https://webstore.ansi.org/preview-pages/ISO/preview_ISO+2859-1-2026.pdf); S: [switching rules summary](https://www.sqconline.com/switching-rules-iso-2859-1)
- P: [Evan Miller, How Not To Sort By Average Rating (2009)](https://evanmiller.org/how-not-to-sort-by-average-rating.html); S: [critique on LessWrong](https://www.greaterwrong.com/posts/SnSYB8CFpjESfRuCm/how-not-to-sort-by-a-complicated-frequentist-formula)
- P: [Anthropic, Reduce hallucinations](https://platform.claude.com/docs/en/test-and-evaluate/strengthen-guardrails/reduce-hallucinations)
- P: [A Survey on LLM-as-a-Judge](https://arxiv.org/pdf/2411.15594), [Self-Preference Bias in LLM-as-a-Judge](https://arxiv.org/pdf/2410.21819), [2026 bias mitigation study](https://arxiv.org/pdf/2604.23178)
- S: [Calibrating an LLM judge with Cohen's kappa](https://oneuptime.com/blog/post/2026-08-31-calibrate-llm-judge-cohens-kappa/markdown), [Galileo](https://galileo.ai/blog/calibrate-llm-judge-human-annotations)
- S: [Turso, multi-tenancy at scale](https://turso.tech/blog/multi-tenancy-at-scale); P: [Python sqlite3 docs](https://docs.python.org/3/library/sqlite3.html); S: [Leifer on SQLite authorization](https://charlesleifer.com/blog/sqlite-database-authorization-and-access-control-with-python/)
- P: [PagerDuty, escalation policy basics](https://support.pagerduty.com/docs/escalation-policies)
- P: [IETF draft, the Idempotency-Key header](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header)
- P: [Twilio, answering machine detection](https://www.twilio.com/docs/voice/answering-machine-detection)
- S: [Ogletree on the EU delay for employment AI](https://ogletree.com/insights-resources/blog-posts/eu-nears-approval-of-agreement-to-delay-rules-for-ai-use-in-employment-decisions/), [Hunton on Illinois HB 3773](https://www.hunton.com/hunton-employment-labor-perspectives/illinois-enacts-new-law-regulating-employer-use-of-artificial-intelligence). Check the Official Journal and the Illinois statute before relying on dates.
