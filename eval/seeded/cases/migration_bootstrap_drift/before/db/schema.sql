-- Bootstrap schema for fresh databases. Keep in step with migrations/.
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE
);
