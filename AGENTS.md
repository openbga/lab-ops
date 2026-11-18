# OpenBGA Lab Ops

Start with [Contributing](CONTRIBUTING.md), the [product scope](docs/product.md),
the [architecture](docs/architecture.md) and the source and tests for the change.
One deployment serves one laboratory and its client organizations.

1. Document the behaviour a laboratory can rely on. Use synthetic examples. The deploying
   laboratory sets its own policies. Retain the project name, licence and notices.
2. Use the existing Django and PostgreSQL conventions. Keep policy in the domain code and
   interface behaviour in views, forms and templates.
3. Keep history attributable and append-only. Corrections record a reason. Use Decimal and
   PostgreSQL numeric for quantities that require exact values.
4. Authorize each server-side read and mutation, including role, capability, organization
   and site scope. Recheck concurrent changes and commit mutations with their audit events.
5. Follow the [interface standards](docs/ui-standards.md) for accessible pages at phone and
   desktop widths. Preserve input and keyboard focus, and handle validation and empty states.
6. Keep secrets, operational logs, uploads, dumps and real laboratory records out of Git.
7. Coordinate changes to shared files, migrations and lockfiles. Preserve other contributors'
   work and keep pull requests focused.
8. Use the repository’s [contributor guides](.agents/skills/) when changing pages, their
   operator help, the changelog or release checks.
9. Follow the verification commands in CONTRIBUTING.md. Use PostgreSQL for authorization,
   concurrency and history tests. Collect static assets before template and browser tests.
10. Update the affected operator help under docs/pages/ when a field, action, permission or
    workflow changes. Record observable changes in changelog.d/ and regenerate CHANGELOG.md.
