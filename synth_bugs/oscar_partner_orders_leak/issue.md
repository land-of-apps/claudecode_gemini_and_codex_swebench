A partner can see other partners' orders on the dashboard

What we observed
================

We host a multi-tenant marketplace: each merchant ("partner") has
their own staff who log into the same Oscar dashboard, see the
orders that contain their products, and process those orders. Each
partner is supposed to see only their own.

A partner reported they can see orders from a different merchant on
the order list. They sent us a screenshot — the orders shown
include line items from a different partner's catalogue (different
products, different SKUs). The partner staff role is supposed to
isolate this.

We checked the user's account in the admin: they're a member of
exactly one Partner (their own), they're not flagged as staff in
the Django auth sense, and the dashboard permission grants them
access via the partner-staff path that other partners also use.
The other partners are NOT seeing the orders cross over — only
this one user, but other partner staff users see the same problem
when we tested.

Steps to reproduce
==================

1. Create two Partners, e.g. "Acme Trading" and "Beta Goods".
2. Create a user, add them to "Acme Trading" only.
3. Place one order containing only Acme line items, and another
   order containing only Beta line items.
4. Log in as the Acme partner-staff user, navigate to the
   dashboard order list.
5. Observe the Beta order shows up in the list.

What we expect
==============

A partner-staff user should see exactly the orders containing
lines from their own partner(s) — never another partner's.

We don't think this is a permissions misconfiguration in the
admin; the user's groups and Partner.users membership are correct.
This looks like a queryset-construction issue somewhere in the
order list view's path. Please find what's filtering wrong and
fix it.

Until we can ship a fix, we're considering disabling the dashboard
for non-admin partner staff entirely, which is going to make a lot
of merchants unhappy. So this needs sorting fairly soon.
