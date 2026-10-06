# IBFS implementation checklist

Working branches: `refactor-improvements` in backend and frontend. No production deployment is included.

Status reviewed on **6 October 2026**: all **36 tracked implementation items** below are complete and committed. This describes the agreed implementation scope, not every possible future accounting feature. Production release work and optional extensions remain explicitly unchecked below.

Latest local validation: 79 backend tests passed, frontend production build passed, and desktop/mobile checks covered WhatsApp preparation, GST summaries and a 51-document CA PDF with exclusions across list pages. These checks used isolated test data; production data and hosting have not been validated.

Preserve the existing simplified accounting contract: quotation / PO / PI do not post; bills and invoices establish obligations; optional automatic payments settle them; challans own stock when enabled; automatic stock remains optional. Extra controls should be optional rather than mandatory stages.

## Accounting and data integrity — first

- [x] Shared Decimal calculations, discount handling and posting / stock rules.
- [x] Atomic account and stock mutations; safe document numbering and reversals.
- [x] Document edits preserve the original automation mode, resynchronize obligations and stock, retain real payments, and detect stale edits.
- [x] Fractional stock movement and aggregated pending / completed quantities.
- [x] Separate cash movement from settlement allocation; vouchers remain printable.
- [x] Charges, discounts, refunds, split payments and payment edits affect settlement correctly.
- [x] Partial / paid / due filters run before pagination and also apply to bulk print.
- [x] Authoritative balances and paginated ledgers; correct incoming / outgoing presentation.
- [x] Balance reconciliation leaves a transaction trail.
- [x] Validate adjustments and protect manual stock corrections.

- [x] Paired account transfers can be reversed once without losing history.

## Controls, usability and desktop — next

- [x] Optional allocation editor for one or multiple bills / invoices.
- [x] Preserve discounts, clear optional fields and protect document drafts during editing.
- [x] Repair authenticated printing and global move-stock actions.
- [x] Desktop navigation, wider workspace, usable desktop dialogs and mobile zoom.
- [x] Linked document stages and direct conversion with inherited data.
- [x] Pagination / search for all major lists and complete selector lookup.
- [x] Dedicated stock history with edit, deletion and print controls.
- [x] Business-timezone dates, useful load / error states and accessible actions.

## Hosting and reliability — before final PDFs

- [x] Supported runtimes, production dependency updates and reproducible builds. Development-tooling audit findings remain listed below.
- [x] Runtime API configuration, PWA caching privacy and production build verification.
- [x] Cookie authentication for the web, logout cache clearing and authenticated uploads.
- [x] Production configuration validation, health checks and fail-fast startup.
- [x] Safe upload cleanup, maintenance / backup instructions and deployment migration checklist.

## Final PDF pass — last, per latest user instruction

Both layouts share the same backend data and pagination. The amount-in-words box and totals breakdown repeat on every page. All summary values stay blank on continuation pages; values appear only on the sheet containing the final item(s).

- [x] Two user-selectable standard invoice / bill templates.
- [x] Print only business / contact / document values that exist; no invented data or default terms. Preview uses the latest saved document or an empty layout.
- [x] Ordinary short bill fits on one A4 page.
- [x] Overflow items continue with the same document top and bottom sections, signature, amount-in-words box and totals fields on every intermediate page.
- [x] Numeric totals, final amount and amount-in-words values appear only where the items finish, on the last page; labels and spaces repeat on prior pages.
- [x] Uploaded letterhead placement and transparent signature work in both templates.
- [x] Visual checks for short, long, unbranded and branded bills / invoices, and bulk PDFs.
- [x] Optional place of supply / reverse charge controls; unspecified values are omitted and explicit No is retained.

## Added reporting and sharing scope

- [x] WhatsApp text and number navigation, with authenticated download and manual PDF attachment.
- [x] CA PDF packs by month/date range, optional document type, and document exclusions across all pages.
- [x] April–March FY/month/custom GST book summary, separate reverse charge and note adjustments, with review flags and full-period totals.
- [x] Keep these features read-only and preserve posting, settlement and challan stock ownership. See [reporting contract](REPORTS.md).

## Release boundaries

Completed code and local checks do not mean a production release has been performed. Follow [deployment and recovery instructions](DEPLOYMENT.md).

### Remaining release and operational work

- [ ] Rotate deployment credentials exposed by the previously tracked frontend `.vne`; replace the actual VM environment. Removing the file from tracking did not rotate secrets or remove Git history.
- [ ] Configure and verify production secrets, allowed hosts, trusted HTTPS origins, server-side API routing, TLS and session-cookie behavior on the actual host.
- [ ] Verify media/static volume ownership for the non-root backend and retain the existing PostgreSQL 15 data volume. A database major-version upgrade is a separate task.
- [ ] Take and verify a release database/media backup, preserve previous image tags, and rehearse restoration in isolated volumes.
- [ ] Rehearse migrations against a restored copy of production data; compare document counts, contact/account balances, payment allocations and inventory before releasing.
- [ ] Review historical payments/vouchers without enough stored document linkage using the allocation editor. Do not guess document matches from amounts alone.
- [ ] Review historical GST flags: missing item amounts, generic tax labels, total mismatches, note references, supplier GSTIN, place of supply and reverse-charge fields. Correct confirmed source data without inventing missing details.
- [ ] Deploy the frontend/backend changes and migrations to production.
- [ ] Perform a production smoke check with test records: login/logout, create/edit/archive, conversion, payment/refund/allocation, transfer reversal, stock/challan movement, both PDF templates, WhatsApp preparation, CA selection and FY GST.
- [ ] Install and verify scheduled maintenance and backups, encrypted off-host copies, retention policy and a recurring restore check.
- [ ] Configure and verify health/failure monitoring for the application, disk usage, backups and maintenance jobs; use a shared throttle/cache backend if running multiple backend replicas.
- [ ] Resolve the development-tooling dependency audit findings without an incompatible runtime/configuration downgrade. The 6 October 2026 frontend audit reports five high-severity findings in the Next ESLint → fast-glob → micromatch → braces dependency chain; these are not marked fixed.

### Optional extensions not implemented or required for this release

These need a separate scope decision. They must preserve the simple daily flow; they are not unfinished parts of the CA PDF pack or manual WhatsApp share.

- [ ] Automated GSTR-2B import/matching, ITC eligibility/reversals and GST payment/filing reconciliation. Current GST is a document book summary, not an eligible-credit or payable-tax calculation.
- [ ] Per-item/mixed GST rates, HSN-wise tax summaries, explicit nil-rated/exempt/export/import classifications and filed-period amendment history.
- [ ] GST return submission and e-invoice/IRN integration through an agreed provider.
- [ ] Bank feeds or statement-import reconciliation.
- [ ] Full double-entry journals, trial balance, profit and loss and balance-sheet reporting, if the product scope expands beyond the existing simplified ledger contract.
- [ ] Multi-company tenancy and associated company-specific data/access controls.
- [ ] A packaged native desktop application/installer, if requested. Desktop browser layout and controls are complete; native packaging/offline desktop operation have not been implemented.
- [ ] Replace the generated frontend README with project-specific local setup and links to the deployment/reporting documentation.

Continue using focused checks and the existing `[FIX]` / `[IMP]` commit convention for further changes.
