# IBFS implementation checklist

Working branches: `refactor-improvements` in backend and frontend. No production deployment is included.

Status reviewed on **6 October 2026**: the original **36 tracked implementation items** are complete, followed by the additional local extensions recorded below. Production VM work was explicitly skipped; full double-entry accounting and multi-company support were deferred. Completion here describes code and local verification, not a production release.

Latest local validation: 96 backend tests passed and the frontend production build passed. Desktop/mobile checks covered mixed-rate create/edit, encrypted offline PDF viewing/draft restoration, WhatsApp preparation, GST summaries and a 51-document CA PDF with exclusions across list pages. Both PDF templates passed short/multipage placeholder checks. The dependency audit has zero findings; focused new-module lint passed, while older modules retain lint debt. These checks used isolated test data; production data and hosting have not been validated.

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

- [x] Supported runtimes, dependency updates and reproducible builds, including a tested narrow Node 24 lint glob adapter; full npm audit has zero findings.
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

## Additional local improvements — complete

- [x] Optional per-item GST rates with backend Decimal previews, cent-exact charge/discount allocation, shared rates retained as the default and edit/conversion preservation.
- [x] Explicit document/line supply classifications and supplier invoice numbers, with contradictory nil/exempt/non-GST tax validation.
- [x] Full-period GST registers and HSN/SAC summaries in CSV/PDF, review flags, signed notes and formula-safe CSV text.
- [x] Payment allocation review with direct access to the existing editor; no guessed historical matches.
- [x] Optional bank-statement and CA-prepared purchase CSV comparisons, duplicate/mismatch/unmatched review and CSV download, with no book mutations.
- [x] Prospective saved document versions and known legacy baseline; archived documents remain readable/printable while edit/reposting bypasses are blocked.
- [x] Installed-PWA desktop/mobile offline workspace: explicitly saved encrypted PDFs and local draft CRUD, returning to the normal online form for posting.
- [x] Database-backed shared production cache provisioned at startup, compatible with existing Compose environments.
- [x] Environment-managed app administrator created or synchronized at startup, including existing databases; passwords are validated/hashed and unchanged credentials preserve their hash.
- [x] Automatic media/static volume ownership initialization in Compose; simple pull/up commands require no manual chmod/chown.
- [x] Verified database/media backup bundles, checksums, accounting snapshots, isolated restore/migration rehearsal and host health/schedule commands.
- [x] Project-specific frontend/backend READMEs and deployment/report/PDF/offline documentation.
- [x] Stock report corrections: expected +/- movements in brackets, physical period closing independent of the last row, and global history product/unit columns without mixing product balances.

## VM handoff — skipped at the user's request

These require the actual host or production data and were not performed. Continue using the existing Compose/private `.env` method; defaults and examples are compatible. See [deployment and recovery instructions](DEPLOYMENT.md).

- [ ] Rotate previously exposed deployment credentials and replace the actual VM environment.
- [ ] Validate production secrets, hosts, HTTPS/CSRF/session behavior and reverse-proxy routing.
- [ ] Verify non-root media/static ownership and retain the existing PostgreSQL 15 volume.
- [ ] Take a production backup, rehearse candidate migrations against restored production data and compare balances/allocations/stock. The tools passed with isolated local PostgreSQL 15 data.
- [ ] Review historical allocation/GST flags with confirmed source documents; missing details must not be invented.
- [ ] Deploy images/migrations and run a production smoke check.
- [ ] Install host schedules, encrypted off-host copies, retention and notifications; verify operational monitoring and recurring restore checks. Commands are provided, not installed remotely.

## Explicitly deferred or excluded

- Full double-entry journals, trial balance, profit/loss and balance sheet: **deferred by user**; preserve current simplified accounting.
- Multi-company tenancy: **deferred by user**.
- GST filing/submission, e-invoice/IRN and other provider validation: **excluded by user**.
- Automated portal GSTR-2B retrieval, bank feeds and third-party reconciliation APIs: **excluded**; local manual CSV comparisons are available.
- Automated eligible-ITC/reversal/tax-payable decisions and filed-return amendment snapshots: **outside current scope**. Book versions and reports do not infer filing status.
- Packaged native desktop installer: **deferred**; desktop browser/installed PWA and offline files/drafts are implemented.

No remaining authorized local implementation item is intentionally deferred. Actual VM/data handoff and the explicitly deferred/excluded features above are not marked complete. Continue using focused checks and the existing `[FIX]` / `[IMP]` commit convention.
