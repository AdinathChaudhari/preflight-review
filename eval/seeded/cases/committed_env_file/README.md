# committed_env_file

Verifier-stage seed. Commit 1 makes settings load `.env.local` and commits that file with an API key. The stored path is `commits/01-load-local-env/dot_env.local` (the loader writes `dot_` as `.`), so this repository does not commit an env file itself.

Before 1.1 the security verifier refuted this class with exclusion 2 ("secrets stored on disk") and precedent 3 ("environment variables are trusted"). `verifier_security.md` v3 overrides both for files committed in the diff. The built-in `secret-files` check also flags the file without any model call.
