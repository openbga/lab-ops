---
date: 2026-11-18
type: added
impact: high
title: Review security settings and name security contacts
area: Security settings
link: security-settings
---
Administrators see what protects sign-in, sessions and patient information, and record the laboratory's security and privacy contacts with a reason.

## Technical notes

Migration `core.0004_labcontact` adds the contacts table. It writes no rows: a contact exists once an administrator records it. The Security settings page reads the deployment's settings and changes none of them. The demonstration seed records two invented contacts at its next reset. The new `docs/safeguards.md` states what the application does for each safeguard the roadmap cites.
