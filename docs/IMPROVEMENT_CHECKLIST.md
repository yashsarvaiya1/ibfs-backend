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
- [ ] Authoritative balances and paginated ledgers; correct incoming / outgoing presentation.
- [ ] Balance reconciliation leaves a transaction trail.
- [ ] Validate adjustments and protect manual stock corrections.

## Controls, usability and desktop — next

- [x] Optional allocation editor for one or multiple bills / invoices.
- [x] Preserve discounts, clear optional fields and protect document drafts during editing.
- [x] Repair authenticated printing and global move-stock actions.
- [ ] Desktop navigation, wider workspace, usable desktop dialogs and mobile zoom.
- [ ] Linked document stages and direct conversion with inherited data.
- [ ] Pagination / search for all major lists and complete selector lookup.
- [ ] Dedicated stock history with edit, deletion and print controls.
- [ ] Business-timezone dates, useful load / error states and accessible actions.

## Hosting and reliability — before final PDFs

- [ ] Supported runtimes and patched dependency versions; reproducible builds.
- [ ] Runtime API configuration, PWA caching privacy and production build verification.
- [ ] Cookie authentication for the web, logout cache clearing and authenticated uploads.
- [ ] Production configuration validation, health checks and fail-fast startup.
- [ ] Safe upload cleanup, maintenance / backup instructions and deployment migration checklist.

## Final PDF pass — last, per latest user instruction

Existing rendering improvements are a foundation; this section is not finished yet.

- [ ] Two user-selectable standard invoice / bill templates.
- [ ] Print only business / contact / document values that exist; no invented data or default terms.
- [ ] Ordinary short bill fits on one A4 page.
- [ ] Overflow items continue with the same document top and bottom sections on every page.
- [ ] Totals and final amount appear only where the items finish, on the last page.
- [ ] Uploaded letterhead placement and transparent signature work in both templates.
- [ ] Visual checks for short, long, unbranded and branded bills / invoices, and bulk PDFs.

## Release boundaries

Run focused checks after logical changes and commit each passing change with the existing `[FIX]` / `[IMP]` convention. Before a production release, back up and rehearse migrations against a copy of real data. Historical payments without a stored invoice reference cannot be assigned automatically; use the allocation editor after reviewing them. Changing credentials and deploying require actual hosting access and are not claimed as completed locally.

Future product decisions such as bank-feed integration, GST filing / e-invoice provider integration, multi-company tenancy and full double-entry financial statements require agreed business scope; do not replace the simplified workflow implicitly.
