# prompt_md_kept

A mixed diff: `app/agent.py` starts parsing replies as JSON while the prompt it loads still asks for prose. The finding is anchored on the prompt file. Before 1.1 the prefilter treated `prompts/**/*.md` as docs; prompt and agent-instruction files are now behaviour (risk tier `source`), so the finding is kept like a code finding.
