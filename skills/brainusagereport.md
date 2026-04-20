---
name: brainusagereport
description: Generate the brainloop HTML day report for today from your local activity database. Runs all DB queries, synthesises periods and widgets, and writes apps/brainloop_YYYY-MM-DD.html.
---

Generate today's brainloop day report.

Follow the full pipeline documented in CLAUDE.md exactly:

1. Auto-detect timezone from the DB using the dual cross-check method (ts_iso vs UTC unix AND SQLite localtime vs utc — both must agree). Never hardcode a timezone offset.
2. Compute local midnight epoch for today's date.
3. Run all 14 DB queries in CLAUDE.md to collect the full day's data.
4. Synthesise into `window.BRAINLOOP_DATA` following all rules in CLAUDE.md — periods (3–5), dayInAPhrase (3–4 sentences, deadpan diary), monkey story (third-person deadpan, no time/duration/platform in story), reading widget (title line 1, when · where line 2).
5. Read `apps/brainloop_day_template.html` as base (never a previous report), patch DATA_UUID `c175b7cb-adee-42a1-bda7-b1807b666c3e`, write to `apps/brainloop_YYYY-MM-DD.html`.
6. Report the output file path when done.
