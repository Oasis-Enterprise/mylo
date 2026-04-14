# Prompt Changelog

Per the implementation plan's versioning gap fix, prompt files are
first-class versioned artifacts. Every change to ``system_prompt.txt``
bumps the version in the first line of that file and adds an entry
here explaining what changed and why.

## 0.1.0 — 2026-04-13 (M4a)

Initial minimal system prompt. Covers identity, tool-use behavior, and
the five security rules from spec §5.2.

Deliberately does NOT include:
- Home topology (layer 2) — added in M4b
- Memory selection (layer 3) — added in M4b
- Task-specific reference examples (layer 4) — added in M4b

The prompt intentionally mentions that write tools are not yet available
so the model doesn't hallucinate them in its responses.
