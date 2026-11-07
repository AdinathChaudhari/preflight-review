# prompt_rule_contradiction

A prompt-only diff: a new hint layer (`prompts/tutor/hints.md`) tells the model to show the final answer, while the base layer it is composed with says never to. No code changes, so before 1.1 the diff was skipped as docs-only and any finding on a `.md` file was dropped. Prompt files are now risk tier `source`, a prompt-only diff gets the full review, and the finding is anchored on the new line with the base layer quoted as evidence.
