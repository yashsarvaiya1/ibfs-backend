# IBFS implementation checklist

Working branches: `refactor-improvements` in backend and frontend. No production deployment is included.

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

- [x] Supported runtimes and patched dependency versions; reproducible builds.
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

## Release boundaries

Run focused checks after logical changes and commit each passing change with the existing `[FIX]` / `[IMP]` convention. Before a production release, back up and rehearse migrations against a copy of real data. Historical payments without a stored invoice reference cannot be assigned automatically; use the allocation editor after reviewing them. Changing credentials and deploying require actual hosting access and are not claimed as completed locally.

Future product decisions such as bank-feed integration, GST filing / e-invoice provider integration, multi-company tenancy and full double-entry financial statements require agreed business scope; do not replace the simplified workflow implicitly.
