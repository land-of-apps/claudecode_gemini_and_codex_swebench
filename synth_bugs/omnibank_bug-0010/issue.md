Customer wires getting accepted on Fed holidays — system thinks "weekday" means "open"

What we observed
================

On Juneteenth (Friday, June 19) we accepted four customer-initiated
wire requests via our standard daytime API even though Fedwire is
closed that day. Three of those requests sat queued through the
holiday and went out at next-day open; one was a same-day need
that the customer had to cancel and re-submit on Monday because we
told them it was being processed. Treasury operations had to walk
each one back manually and reach out to the originators.

Looking at the audit log, the system marked all four submissions
as "open window — accepted" at the time of submission. That's the
status you'd expect on a normal weekday morning. Fedwire was, in
fact, closed for customer-initiated activity on Juneteenth (and is
also closed on the other federal holidays that fall on weekdays:
Memorial Day, July 4 when it falls on a weekday, Labor Day,
Thanksgiving, Christmas, MLK Day, Presidents' Day, and so on).
The published Fed holiday calendar lists each one explicitly.

We dug into pre-regression behavior. The customer-wire window
*used* to consult the Fed holiday calendar — submitting on
Juneteenth (or any other Fed holiday) returned "closed" the same
way submitting on a Saturday does. After the most recent release,
that calendar consultation appears to be gone: only the day-of-
week check remains. Saturdays and Sundays still return closed
correctly, but Fed-observed holidays falling on a weekday return
"open."

Steps to reproduce
==================

1. In a controlled environment, freeze the application clock to
   2026-06-19 11:00 a.m. ET. (Juneteenth is a Friday in 2026.)
2. Ask the system whether the wire window is open at that moment.
3. The system says yes.
4. For comparison, freeze the clock to 2026-06-20 11:00 a.m. ET
   (Saturday) — the system correctly returns no.

The fault is specifically: weekday-but-Fed-holiday → false-open.

What we expect
==============

Customer-wire-window should return "closed" on any of:
- Saturday
- Sunday
- A Federal Reserve-observed holiday (which our internal calendar
  knows about — that's the lookup that used to be wired in here).

The current code only handles the first two.

What actually happens
=====================

The system reports "open" on Fed holidays as long as they're not
weekends. Juneteenth, MLK Day, Presidents' Day, Memorial Day,
Independence Day (when on a weekday), Labor Day, Columbus Day,
Veterans Day, Thanksgiving, and Christmas Day are all impacted.
Roughly nine to ten weekdays per year incorrectly accept wire
submissions.

Impact
======

Four wires improperly accepted on Juneteenth alone. We're
expecting more on July 4 (next Fed holiday on a weekday this
year) and the rest of the calendar's holidays unless this is
fixed. Each one creates an operational fire-drill: we have to
reach the originator, explain the delay, and depending on their
business cycle either hold the request or have them cancel and
re-submit.

The Fed-holiday awareness used to be in the wire-window check
and needs to be back. The shared business-calendar lookup that
already powers other holiday-aware logic (settlement date math,
ACH cutoffs) is the right thing to consult here too — same
calendar, same source of truth.
