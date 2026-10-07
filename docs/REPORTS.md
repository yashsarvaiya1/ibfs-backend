# CA documents, GST and sharing

Access **Reports & CA exports** from the desktop sidebar or the account menu on mobile. These actions are read-only and do not create payments, ledger entries or stock movements.

## CA document pack

Per the requested CA handoff, this is a single PDF of existing documents, not an Excel workbook, Tally import or financial-statement generator.

Choose a month, financial year (1 April–31 March) or custom inclusive dates. All active document types are included by default; optionally choose one type. Documents appear oldest first, with a preview link. All matching documents across every list page are initially selected. Uncheck exceptions, clear the selection to choose specific documents, or select all again. Download combines only that selection into one PDF using the saved Classic/Modern print template, business letterhead and signature.

The list has a server timestamp. New documents added after opening it are not silently included. Missing/archived documents or selected document edits detected before generation return a refresh/review message. An explicit empty selection exports nothing. Refresh starts a fresh selection. Generation renders batches of 100 documents and merges their PDF pages; long invoices retain their repeated headers and blank continuation totals, with values only on each document's final page. Files remain authenticated and are not exposed through public links.

APIs: `GET /api/reports/ca_documents/`, `POST /api/reports/ca_export/`. Both accept inclusive `date_from`/`date_to` or `fy` (starting year), optional `types`; the list supports `page`/`page_size`, and `as_of` freezes creation membership. Export accepts either explicit `ids` or `excluded_ids`, plus optional `expected_count` and the list's `as_of`. The UI sends both snapshot and count to catch stale selections.

## GST book summary

`GET /api/reports/gst/` uses active document dates, including both period boundaries. Financial-year defaults follow April–March. Summary and monthly amounts cover the whole period even when the document breakdown is paginated or filtered with `review_only=true`.

- Invoice: sales/output GST. Bill: purchase GST recorded in books.
- IBFS CN: customer/sales return, reducing output GST. IBFS DN: supplier/purchase return, reducing purchase GST. This reflects the existing signed ledger/stock flow; it does not assume every statutory debit note is a purchase return. Notes require an active matching invoice/bill for the same contact and a consistent reverse-charge flag. Unlinked or conflicting notes go to review.
- Quotation, PO and PI do not post. Challans move stock; payments and vouchers settle cash. None is counted again as GST.
- Document-wide tax remains the default: item amounts + charges − discount, using Decimal rounding. Optional item mode applies each saved line’s rates after allocating document charges/discount proportionally in cents; rounded line taxes are aggregated by name/rate. The create/edit form previews those same backend calculations.
- CGST, SGST, IGST, UTGST and cess are separate. Generic GST is retained without an invented split. Unknown tax labels are shown as other tax and excluded from GST. Reverse-charge documents are separate from normal sales/purchase totals.
- Missing item amounts/fast-entry details, invalid calculations, total mismatches and conflicting components are excluded from normal totals with a reason. Missing GSTIN/place of supply and unspecified reverse charge are review flags; an unspecified flag is treated as normal in this book comparison, expressly pending review. Nil-rated/exempt/export classifications are not inferred from a zero tax amount.

**Book GST difference** is normal output GST minus normal purchase GST. It is not eligible ITC, tax payable, a component-offset calculation, portal reconciliation or a GST return. Purchase GST must be checked against GSTR-2B and eligibility conditions; RCM payment/credit, amendments, import/export treatment and filing-period differences need CA review. Historical document edits are reflected in the current saved book data. Prospective saved versions show creates/edits and a known baseline on the first tracked legacy edit; they are not filed-return snapshots.

Primary practice references checked for this implementation:

- [GST portal: GSTR-2B and reconciliation](https://tutorial.gst.gov.in/userguide/returns/FAQ_gstr2b.htm): books matching, duplicate-credit prevention and separate reverse-charge handling.
- [GST portal: GSTR-1 preparation](https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm): financial year/quarter/month selection and invoice/note particulars.
- [CBIC: accounts and records](https://cbic-gst.gov.in/accnt-record-rules.html): document registers and traceable supporting records. A PDF pack supports the CA's work; it does not replace complete statutory books.

## WhatsApp sharing

Document detail and print screens offer **Share on WhatsApp**. Choose a saved recipient or enter a number, review/edit the prepared text, download the authenticated PDF, then click **Open WhatsApp**. Attach the named file manually from Downloads and send when ready. This uses [WhatsApp click-to-chat](https://faq.whatsapp.com/5913398998672934/?locale=en_US), with no WABA integration, public PDF URL or automatic sending.

Ten-digit numbers use India's country code; explicit international numbers use `+` or `00`. Invalid numbers cannot open a link. A ten-digit Indian number starting with `91` still receives the country code. The link opens directly from a user click, avoiding delayed popups. Amounts come from saved data; challans and missing amounts omit the total. Copy-message and download-again actions remain available.

## GST/HSN exports and optional particulars

The GST register exports the entire selected period to CSV or a landscape A4 PDF, including book totals and review reasons. `GET /api/reports/gst_export/` accepts the same period and optional review filter plus `export_format=csv|pdf`. Do not use DRF's reserved `format` query parameter. Numeric CSV values preserve their signs; text that could become spreadsheet formulas is escaped.

`GET /api/reports/hsn/` groups saved HSN/SAC, unit, rate and explicit supply classification by sales/purchase/RCM bucket. `hsn_export/` downloads the whole summary as CSV/PDF. It does not invent missing codes, units, quantities or nil/exempt treatment. Invalid GST calculations appear in an excluded-document list. Shared document taxes/charges/discounts are allocated proportionally in cents for the HSN view, preserving document rounded tax totals; item mode uses its own line calculations. Unknown particulars are review flags. This is a CA-readable book summary, not a portal-upload JSON schema.

Tax details optionally capture taxable/nil-rated/exempt/non-GST/export/import treatment at document or line level, plus a supplier's original invoice number. Line classifications override the document classification for HSN grouping. Nil-rated/exempt/non-GST lines cannot carry positive tax rates; mixed taxable/exempt lines use item mode. Export/import labels alone do not establish statutory treatment, refund entitlement or filing sections.

## Allocation review

`GET /api/reports/allocation_review/` lists actual payments with unallocated amounts or inconsistent saved allocations for the period. Open a payment to use the existing allocation editor. This changes allocation only when the user saves it; the report never guesses matches or creates cash movement.

## Manual CSV comparisons

**Compare CSV** is optional. Download a template, prepare the file and upload it for read-only review. `comparison_template/` and `compare_csv/` accept `kind=bank|purchase`; bank comparisons also require an account. Files are limited to 5 MB/5,000 rows and are not stored on the server. Dates use `YYYY-MM-DD`; amounts require explicit finite values with at most two decimals.

- Bank: signed statement amount/date are compared to actual/transfer cash entries in the selected account. Incoming is positive, outgoing negative. A unique match is a candidate requiring reference review; repeated amounts/dates are ambiguous. No imported payment or reconciled-status claim is created.
- Purchase: CA-prepared invoice CSVs (for example derived from GSTR-2B) match bills using supplier GSTIN, supplier's original invoice number and date, then compare taxable/component amounts. Internal IBFS numbers never substitute for missing supplier numbers. Agreement does not establish eligible ITC. Notes/amendments, filing-period timing and RCM payment need CA review.

Results and unmatched book entries are shown separately, with document/payment links and an optional CSV download. No portal login, provider API, bank feed, return submission, IRN validation or automated posting is used.

## Stock PDFs

Inventory → Print report → Generate PDF → Save PDF downloads current quantities for all active products (or the current search/low-stock filter). Select products first to download only those product snapshots. The export covers the matching backend list, not just the visible page.

Open a product → Ledger History → Print to choose optional inclusive dates and download its ledger. Stock history also offers product/type/date filters and Print history. Expected movements print as `(+5.00)` / `(-5.00)`; physical movements print as `+5.00` / `-5.00`. Only physical movements affect running/opening/closing stock. Period balances include all physical movements even when the displayed rows are further filtered, and closing stock remains visible when the final row is expected or the period is empty. The header's current stock is today's saved quantity; closing stock belongs to the selected period.

Global history prints product names and each product's unit without a combined balance across products. Selecting several products for separate ledgers in one PDF is not implemented; the Inventory multi-selection export is a current-quantity snapshot.

## Separate financial-year business report

`GET /api/reports/financial_year/?fy=2026` summarizes 1 April 2026 through 31 March 2027. It shows invoices less credit notes, bills less debit notes, expense documents, separately recorded Other income, document counts and all twelve monthly activity rows. Document totals include tax; calculable pre-tax figures are shown separately and amount-only or inconsistent documents are listed for review. Actual account receipts/payments follow cash movement dates and exclude internal transfers, record-only entries, quotations, orders and challans. This is a business activity summary, not a profit/COGS calculation or a GST return. PDF/CSV exports use `/api/reports/financial_year_export/` with the same `fy` and `export_format=pdf|csv`.

The separate GST screen defaults to a month and continues to support any date range (including a full FY). It accumulates recorded sales/purchase GST after credit/debit notes, with reverse charge, unclassified tax and review exclusions separately visible. Purchase tax is not automatically eligible ITC and book GST difference is not tax payable.

## Invoice discounts and charges

`discount` remains the saved currency deduction. Optional `discount_percentage` preserves a rate from 0 to 100; the backend calculates its deduction from the items subtotal, rounds it to cents, then applies charges and tax. Existing documents default to amount mode. Percentage mode requires item details; clearing the percentage selects amount mode. Changing items recalculates a saved percentage. Preview, posting, edits, PDFs and GST/HSN reports share this Decimal calculation. Document charges and discounts continue to be allocated proportionally across per-item taxable bases, conserving cents.

Invoice-time discounts reduce taxable value and supply-related incidental charges enter it, consistent with [CGST Section 15](https://taxinformation.cbic.gov.in/content-page/explore-act/1000284/1000001). This discount input is for a discount recorded on that document. It does not reinterpret cash settlement adjustments as an automatic reduction in GST; post-supply discounts need their applicable credit-note/ITC conditions reviewed separately.

Per-item discounts are stored with the existing JSON item details: currency `discount`, or optional `discount_percentage` for that item. Item amounts remain the gross quantity/rate value; each item discount is applied once to produce its net amount. The overall discount then applies to the remaining items subtotal, followed by charges and tax. Per-item GST and HSN allocations use these net values. PDFs show each item's net amount and its own discount detail. Items without discount fields retain their existing calculation. No extra migration is needed for item discount fields.

## Ledgers and document edits

IBFS keeps its existing signed balances. A negative contact balance is receivable (they owe us); a positive contact balance is payable (we owe them). Screen and PDF debit/credit columns interpret those signs consistently:

| Entry | Contact ledger | Payment account ledger |
| --- | --- | --- |
| Invoice or debit note | Debit | No cash entry until payment |
| Bill or credit note | Credit | No cash entry until payment |
| Receipt / cash receipt voucher | Credit | Debit (money in) |
| Payment / cash payment voucher | Debit | Credit (money out) |
| Interest/charge we receive | Debit | No cash entry |
| Interest/charge we pay | Credit | No cash entry |
| Waiver while receiving | Credit | No cash entry |
| Waiver while paying | Debit | No cash entry |
| Income receipt | No contact debit/credit or balance change | Debit |
| Expense payment | No contact debit/credit or balance change | Credit |
| Account transfer | No contact debit/credit or balance change | Debit in destination, credit in source |

A receipt of 80 plus a waiver of 20 settles an invoice of 100, while cash increases only by 80. A payment of 80 plus a waiver of 20 settles a bill of 100, while cash decreases only by 80. A charge of 20 with a payment/receipt of 100 allocates 80 to the original document and 20 to the charge. Waivers retain the existing `discount` API value for compatibility; document item discounts still use their existing tax-base calculations.

Date ranges are inclusive and invalid ranges return validation errors. Opening and running balances use preceding transactions, including hidden rows and previous pages. Contact reports exclude income, expenses and transfers from the contact balance; account reports include both in the cash balance. Contact ledger prints include obligations even when automation is enabled. Prints query all matching entries rather than a loaded UI page. An empty period can still show its opening/closing balance.

Editing a contact opening balance changes the starting balance only. Editing a financial document recalculates its obligation and settlement status; real cash payments remain intact. Expected stock is recalculated; automatic stock corrections update quantity and follow the corrected document date. Manual deliveries retain their actual quantities and dates. When challans own inventory, bills/invoices retain that responsibility split. Quotation/order edits remain non-posting.


Income documents record cash receipts that are not sale invoices. They appear in CA document bundles and FY Other income, and their cash entry appears once in Cash received. They do not enter sales GST/HSN or settlement/allocation review. Optional contacts identify the receipt source without changing party balances; account ledgers show money in on the debit side. Income is edited/deleted through the document so the cash entry stays consistent.

Account opening-balance edits recalculate current and historical ledger standings without inserting an adjustment transaction. Existing payments, transfers and allocations remain intact. Reconcile balance and Adjust remain separate controls for recording an actual correction, interest or charge.
