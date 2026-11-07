# migration_bootstrap_drift

A new migration adds `accounts.status` with a CHECK constraint. The hand-maintained bootstrap schema (`db/schema.sql`) is not updated, and `db/bootstrap.py` builds fresh databases from it and marks every migration applied, so a fresh database never gets the column and `suspended_accounts` raises. The finding is anchored on the added migration line with the snapshot cited as evidence. Covered by the data_concurrency lens (bootstrap parity).
