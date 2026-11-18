---
date: 2026-11-18
type: added
impact: high
title: Record nonconformances and corrective actions
area: Nonconformances
link: nonconformance:list
highlight: true
---
Track a problem from containment to an effectiveness check, with dated actions and closing by another supervisor. Administrators switch the module on in the laboratory profile.

## Technical notes

Migrations `core.0003_labmodule` and `nonconformance.0001_initial` add the module switch and the new tables. Every optional module starts switched off, so an upgraded laboratory sees no change until an administrator switches it on. The demonstration seed switches it on and adds four synthetic records at its next reset.
