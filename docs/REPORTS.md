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
- Document-wide tax base is item amounts + charges − discount, using the existing Decimal rounding. Taxes are not reallocated to individual HSNs or guessed as mixed item rates.
- CGST, SGST, IGST, UTGST and cess are separate. Generic GST is retained without an invented split. Unknown tax labels are shown as other tax and excluded from GST. Reverse-charge documents are separate from normal sales/purchase totals.
- Missing item amounts/fast-entry details, invalid calculations, total mismatches and conflicting components are excluded from normal totals with a reason. Missing GSTIN/place of supply and unspecified reverse charge are review flags; an unspecified flag is treated as normal in this book comparison, expressly pending review. Nil-rated/exempt/export classifications are not inferred from a zero tax amount.

**Book GST difference** is normal output GST minus normal purchase GST. It is not eligible ITC, tax payable, a component-offset calculation, portal reconciliation or a GST return. Purchase GST must be checked against GSTR-2B and eligibility conditions; RCM payment/credit, amendments, import/export treatment and filing-period differences need CA review. Historical document edits are reflected in the current saved book data, not reproduced as filed-return snapshots.

Primary practice references checked for this implementation:

- [GST portal: GSTR-2B and reconciliation](https://tutorial.gst.gov.in/userguide/returns/FAQ_gstr2b.htm): books matching, duplicate-credit prevention and separate reverse-charge handling.
- [GST portal: GSTR-1 preparation](https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm): financial year/quarter/month selection and invoice/note particulars.
- [CBIC: accounts and records](https://cbic-gst.gov.in/accnt-record-rules.html): document registers and traceable supporting records. A PDF pack supports the CA's work; it does not replace complete statutory books.

## WhatsApp sharing

Document detail and print screens offer **Share on WhatsApp**. Choose a saved recipient or enter a number, review/edit the prepared text, download the authenticated PDF, then click **Open WhatsApp**. Attach the named file manually from Downloads and send when ready. This uses [WhatsApp click-to-chat](https://faq.whatsapp.com/5913398998672934/?locale=en_US), with no WABA integration, public PDF URL or automatic sending.

Ten-digit numbers use India's country code; explicit international numbers use `+` or `00`. Invalid numbers cannot open a link. A ten-digit Indian number starting with `91` still receives the country code. The link opens directly from a user click, avoiding delayed popups. Amounts come from saved data; challans and missing amounts omit the total. Copy-message and download-again actions remain available.
