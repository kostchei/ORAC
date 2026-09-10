# ORAC workspace rules

You are working inside the ORAC repository. Keep changes narrow, explain the
reason for each change, and run the smallest relevant test set before calling
work complete.

- Preserve the existing broker, council, approval, audit, and path-safety
  boundaries. Do not add a privileged shortcut around them.
- Treat `.orac/` as local runtime state. Do not commit its databases,
  credentials, tokens, model caches, or generated board files.
- Use the existing Python entry points (`python -m orac.cli ...` or `orac ...`)
  and keep LM Studio calls on its local OpenAI-compatible API.
- Prefer `apply_patch`-style small edits and inspect the surrounding code before
  changing safety-critical files.
- For model work, use the configured LM Studio model slot and report the exact
  model id and endpoint used.
