# Offline PWA workspace

The desktop/mobile PWA exposes **Offline files** at `/offline`, outside the authenticated dashboard shell. Only files explicitly saved by the user are kept offline. API and private media requests remain network-only; cached navigation does not expose the accounting database.

## Save and use

1. While signed in online, choose **Save offline** on a document PDF, report PDF, CA pack or document draft.
2. Create a separate vault password (at least eight characters), or unlock the existing vault. Setup requires an online signed-in user. The vault belongs to that username.
3. Offline, open `/offline`, unlock with that password, then open/download/delete saved PDFs or create/edit local drafts. No server session is needed to unlock an existing vault.
4. Reconnect and choose **Continue online** on a draft. The normal create form restores its saved details and performs server validation before posting. A successful create removes the local draft. Reconnection does not reload the page or automatically submit drafts.

The vault uses IndexedDB with AES-GCM encryption and a PBKDF2-SHA256-derived nonextractable key. File names, document metadata and contents are encrypted. Vault owner/configuration remains readable locally. Keys stay only in browser memory; reload, page exit or Lock requires unlocking again. Lock broadcasts to other tabs. Logout/session-expiry handling clears local copies as well as query caches.

There is no password recovery. Clearing the vault deletes the device copies after confirmation. Browser storage can be evicted, and each saved file is limited to 50 MB; download important files separately. Encryption protects stored files while locked, not an unlocked browser or a compromised device. Server backups do not include browser vaults.

Offline drafts never allocate payments, update stock, create ledger entries or claim server document numbers. Reference/contact IDs can become stale while offline and must be reviewed online. There is no queued background posting or conflict-merging engine. This is installed-PWA desktop support; a native installer is deferred.
