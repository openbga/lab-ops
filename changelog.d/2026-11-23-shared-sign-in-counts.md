---
date: 2026-11-23
type: security
impact: medium
title: Failed sign-ins count the same in every server process
area: Security settings
link: security-settings
---
The limit on failed sign-ins now counts every attempt once, however many server processes the deployment runs.

## Technical notes

`CACHES` is now `core.cache.SharedCache`, Django's database cache in the table `lab_ops_cache`, created by migration `core.0007_shared_cache_table` (run `migrate`; `createcachetable` is not needed). Keys are stored as HMACs made with `SECRET_KEY` (`core.cache.keyed_digest`), so the table holds no name or address; rotating `SECRET_KEY` starts the counts afresh. Every write removes expired entries, a live one is never removed to make room, and an entry lasts at least as long as asked. A deployment that replaces `CACHES` with a per-process cache gives each worker its own counts again, and Security settings says the configuration changed.
