# Merchant Payments and Reconciliation

A local, multi-merchant **sandbox** built with Django templates and PostgreSQL. All amounts are simulated USD cents. Checkout accepts synthetic scenario tokens; beneficiaries use simulator-issued `acct_demo_` identifiers. This application has no live payment integration.

Merchants can issue a single-use checkout link, authorize and make one final capture, refund principal, inspect immutable journals, receive signed settlement activity, propose payouts with a different owner's approval, and respond to disputes. Platform staff can review merchants, import manifest-verified processor and bank CSVs, match exact evidence, review corrections, close and reopen accounting periods, inspect audit and recovery work, and export protected reports.

## Run locally

Prerequisites: Docker Engine or Docker Desktop with Compose supporting `!reset` overrides (Compose 2.24.4 or newer), a POSIX shell, and available loopback ports. The tested architecture is Linux ARM64 inside Docker Desktop on macOS. The specified performance reference requires four CPUs, 8 GiB RAM, and 10 GiB free storage; functional checks on a smaller shared host do not establish those capacity targets.

From this directory:

```sh
sh scripts/local.sh init
sh scripts/local.sh migrate
sh scripts/local.sh seed-demo
sh scripts/local.sh up
sh scripts/local.sh health
```

`init` builds the image, generates an environment UUID and independent secrets in ignored `.local/environment`, and refuses to overwrite existing configuration. `migrate` provisions distinct migration, runtime, and purge roles in the separate main and simulator databases, then applies their migrations. Migrations never run as an HTTP import side effect.

The default application URL is `http://localhost:8000`; Mailpit is `http://localhost:8025`. If another local application uses these ports, change `WEB_PORT` and `MAIL_PORT` in `.local/environment`, then run `up` again. The working installation created during development uses **http://localhost:8180**. Only the web and captured-mail interfaces are exposed, on loopback.

Generated fictional account passwords and authenticator seeds are in `.local/demo-access.txt`, readable only by its owner. Add a user's seed to an authenticator and sign in with its generated password and a current code. Repeated TOTP counters are rejected, including between sign-in and sensitive approval. Seeds and passwords are never committed. Seed creation is explicit and idempotent; it refuses to silently replace existing user credentials.

The demo business is Juniper Goods. Its owner, finance operator, analyst, and separate platform operations, reconciliation, finance approver, and auditor identities use `example.test` addresses. Registration instead creates a pending merchant: verify its captured email, enroll an authenticator, and have platform operations activate it. Invitations bind the exact fictional email, merchant, and role. Only `example.test` and `demo.invalid` email domains are accepted.

```sh
sh scripts/local.sh logs       # last 100 lines, redacted bearer paths
sh scripts/local.sh stop       # preserve this project's volumes and configuration
sh scripts/local.sh restart    # fence the previous full run before starting new work
```

The main database, simulator database, and Mailpit captures live in separate project-scoped Docker volumes. Do not remove them to troubleshoot a timeout. Never point verification at these development resources.

Compose restarts the four named application processes after unexpected exits; an explicit stop keeps them stopped. Workers retry transient database outages with bounded delays. Restarting a process preserves the prepared run, accepted operation keys, leases, and reservations; only the coordinated `restart` command prepares a replacement run.

## Follow a payment

1. Sign in as finance. Open Payments → Create payment link. Choose a fixed USD amount, automatic or manual capture, and an expiry. Save the displayed URL; it is shown once.
2. Open the link in a separate browser session. Choose a synthetic scenario and confirm. No card number or bank information is requested. A GET does not create a payment.
3. Inspect its saved receipt and merchant payment detail. Manual authorization allows one final capture, including a partial capture. A successful $100 capture posts $100 principal and $3.20 processor fee; fees are not returned on refund.
4. A refund needs an amount and reason. Active and uncertain refunds retain principal reservations. An open dispute prevents another refund. A later refund can create a visible merchant deficit after money was paid out.
5. Platform operations can flush unbatched synthetic processor activity. The independently committed transfer moves clearing to simulated bank control. A capture alone is not settled cash.
6. Finance proposes the merchant's displayed synthetic beneficiary. A different owner confirms password plus a fresh TOTP code, then approves. Finance proposes a payout; a different owner reviews and approves its frozen amount and destination. Approval reserves available settled funds; the worker rechecks current authority and funds before dispatch.
7. Reconciliation staff import the simulator's exact processor-economic, processor-transfer, and bank statements with their immutable manifest metadata. Preview, confirm, then run exact matching. Matching creates evidence allocations, never financial journals. Differences remain visible until corrected evidence or an approved authoritative correction resolves them.
8. A past UTC calendar month can close only after manifest completeness, matching, transfers, unresolved effects, balances, and control totals pass. A separate finance approver revalidates the snapshot. Reopen requires another maker/checker decision; prior reports remain retained.

`UNKNOWN` means the processor may already have completed the action. Inspect the existing operation; do not create a replacement attempt. Lookup atomically returns a committed effect or installs a terminal no-effect tombstone. Closing a browser tab never cancels accepted work. Cancellation can prevent only provably unsent work.

## Roles and financial controls

Merchant owners manage settings and teams and approve proposals made by another person. Finance operators issue links, capture, refund, propose beneficiaries and payouts, and respond to disputes. Analysts have read access. Platform operations manages merchant lifecycle and synthetic controls; reconciliation analysts manage evidence and propose adjustments/close; finance approvers check adjustments and close/reopen; auditors read scoped records.

Every protected request rechecks current authority. Wrong-tenant IDs return 404, and insufficient same-tenant roles return 403. Financial admission and dispatch coordinate actor, merchant, membership, and financial records with PostgreSQL locks. Sessions expire after 30 minutes idle or 12 hours absolute and carry a revocable authentication epoch. Sensitive approvals require a five-minute password/TOTP step-up.

Accounting alone posts double-entry journals. PostgreSQL deferred constraint triggers enforce balance and scope, and immutable guards reject journal or entry edits. Runtime roles cannot perform schema changes or purge financial data. Source identities and admitted provider keys remain stable through retries. Local approvals and cancellations retain scoped action receipts: equivalent retries inspect the same result, conflicting reuse is rejected, and expired response keys remain reserved. Audit streams are hash linked; there is no external immutable anchor, and a database owner remains trusted.

Payout availability is `max(0, min(merchant payable, settled bank control) - active reservations)`. Each merchant is independent. Clearing receivables, another merchant's funds, and an uncertain effect cannot fund a payout. Mandatory audit and durable notification admission share protected local transactions. SMTP delivery can duplicate after a lost acknowledgment; this does not duplicate financial effects.

## Recovery and local data handling

Readiness distinguishes reads, financial admission, callbacks, worker, reconciliation, notification, and recovery. A responding health endpoint alone does not establish payment readiness. Missing triggers, unsafe runtime roles, stale workers, simulator mismatch, and restoration hold disable dependent financial work while retained results remain inspectable.

Run operator commands through the explicitly configured project. Examples:

```sh
docker compose --env-file .local/environment run --rm web python manage.py inspect_keys
docker compose --env-file .local/environment run --rm web python manage.py reconcile_operations --limit 50
docker compose --env-file .local/environment run --rm web python manage.py restore_hold --operator local-operator --reason 'Investigating a restored database'
```

`recovery_inventory --email OPERATIONS_EMAIL --reason REASON` authenticates a current operations user using password and a fresh authenticator code. `approve_restore_release --inventory-id ID --email FINANCE_EMAIL` requires a different current finance approver. Inventory compares independent provider operations, sources, event positions, local journals, reservations, and retained admission. A missing record with no safe reconstruction remains a hold finding. Release cannot guess a no-effect result or approve its own inventory.

```sh
sh scripts/local.sh backup
sh scripts/local.sh restore .local/backups/main-TIMESTAMP.dump .local/backups/simulator-TIMESTAMP.dump
```

Backup quiesces all application and simulator processes, retains separate database dumps in an owner-readable local store with an environment sentinel, and restarts the run. Restoration requires typing the exact environment UUID and replaces only the selected project's databases. Restored state remains held until inventory and separate finance approval. Keep backups outside source delivery; the selected local retention is 30 days. Retain private configuration and encryption keys separately with the same protection; database dumps do not contain their decryption keys. Backup/restore is a local recovery mechanism, not a production recovery qualification.

`cleanup_retention --environment-id UUID --operator NAME --reason REASON [--backup-root PATH]` clears expired transient uploads, export bytes, token delivery copies, and eligible diagnostics. Financial history is retained for active merchants and for 365 days after closure. Merchant closure checks balances and deficits, disputes, proposals, unknown operations, required evidence, and periods. Account deactivation preserves pseudonymous financial actor IDs.

`purge_closed_merchant` exposes explicit propose, approve, retire, execute, and inspect phases. It requires an eligible closed merchant, separate authenticated operations and finance identities, an explicitly mounted backup-store inventory, permanent simulator retirement and completed simulator purge, and separately supplied `mprp_purge` credentials. Web and ordinary workers never receive purge credentials. Read command help before operating it. Interrupted purge resumes from its retained receipt; retired namespaces cannot be reused. Do not bypass immutable guards or manually delete tenant rows.

There is no automatic destructive reset. Removing development volumes is an explicit local operator action and is never part of startup, normal stop, or verification.

## Source navigation

| Package | Responsibility |
| --- | --- |
| `accounts/` | Custom users, hashed single-use tokens, encrypted TOTP, recovery codes, session epochs, account views |
| `merchants/` | Merchant control rows, current membership and role checks, settings, teams, closure requests |
| `payments/` | Single-use links and scoped guest checkout, authorization/capture/void/refund admission and observations |
| `accounting/` | Economic sources, source-unique journal posting, balances, periods and PostgreSQL ledger guards |
| `settlements/` | Signed batch observations, bank movements, beneficiary versions, dual approval and payout reservations |
| `reconciliation/` | Strict CSV parser, manifest confirmation, exact allocations, exceptions, correction and close/reopen controls |
| `disputes/` | Ordered principal events, response admission, deadlines and outcome application |
| `operations/` | Durable jobs and leases, HTTP adapter, inbox/outbox, audit, reports, readiness, restoration and retention |
| `processor_sim/` | Independent synthetic provider records, immutable keys and tombstones, epochs, event feed, manifests, settlement and dispute scheduler |
| `config/` | Main and restricted simulator settings, typed local bindings and route composition |

Domain rules live in feature `domain.py` modules. `operations/worker.py` owns claims and result-generation guards; `operations/provider.py` owns the fixed-origin signed HTTP boundary; `operations/recovery.py` owns inventory and epoch reconciliation. Templates are feature-namespaced; shared layout and form rendering are in `templates/`; retained CSS and progressive JavaScript are in `static/operations/`. No frontend build service or remotely hosted asset is required. The normal Django administration route is deliberately absent. Sensitive account errors are always redacted; DEBUG must be 0. Local static files use the development server’s explicit `--insecure` static-file option.

## Verification and maintenance

```sh
sh scripts/verify.sh
```

The verification wrapper generates a unique `mprp_verify_` project with independent secrets, networks, ports, volumes, and databases. It builds image-contained source, applies migrations, starts both runtimes and workers, runs Django/PostgreSQL checks, a real HTTP financial journey, and Chromium/Firefox/WebKit browser journeys, then tears down only its own recorded resources. Logs and screenshots are stored outside source in a sibling `MPRP Implementation Records` directory; the temporary configuration is removed. Override that external destination with `MPRP_EVIDENCE_DIR` if needed. Individual steps have finite deadlines. Verification never binds development source into the application image and never deletes development volumes.

The browser image installs locked Playwright browsers and OS libraries. Each engine runs in a fresh process with a finite deadline and phase diagnostics. Browser checks include real password/MFA, checkout with JavaScript disabled, capture, protected screens at 360/768/1440 widths, export download, and a distinct-owner payout approval. Automated browser checks do not replace a human screen-reader assessment or establish WCAG certification. A separate external implementation report records actual execution scope, failures, limitations, source identity, and remaining acceptance work; source code is not itself evidence that every specified adverse case passed.

Runtime dependency inputs are `requirements.in` and `requirements.lock`; development tools use `requirements-dev.in` and `requirements-dev.lock`. Both locks include hashes. Python 3.13, PostgreSQL 17, and Mailpit images are pinned by digest in Docker inputs. To update locks, use Python 3.13 with pip-tools 7, `pip-compile --allow-unsafe --generate-hashes`, review licenses/advisories, rebuild images, and rerun affected checks. Do not regenerate them through an unrelated host Python. Retained third-party package license notices are in their installed distributions.

To change schema, stop admission and workers, retain accepted unknown work, generate and inspect an explicit feature migration, run migrations through the owner role, verify PostgreSQL guards and control totals, and restart all processes coherently. There is no promise of mixed-version rolling operation. Use `python manage.py verify_contract` for Django/model/migration consistency; it does not assert completion of all behavioral acceptance cases.

Production hosting, TLS termination, managed backups, live financial rails, financial compliance, public publishing, and production capacity are deferred. This local sandbox cannot become a live-money product by changing a URL or flag.

Secret rotation uses a coherent stop/restart. Set a new active `FACTOR_ENCRYPTION_KEY`, retain the old key in `FACTOR_ENCRYPTION_PREVIOUS_KEYS`, and run `reencrypt_factor_secrets --operator NAME --reason REASON` before retiring old decryption keys. Service transport keys have explicit IDs; previous-key configuration requires recorded activation and expiry instants with a maximum 24-hour interval. Verification checks expiry on every request. Reconcile pending work and remove compromised keys before restoring admission; never change an admitted operation's key or payload to rotate credentials.
