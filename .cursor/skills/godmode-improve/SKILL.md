---
name: godmode-improve
description: Self-improving meta-skill. Detects pain points from run diagnostics and user feedback, proposes targeted edits to skill files, applies with approval, and logs changes. Can improve itself. Invoke when the user asks to improve, tune, or evolve the job-finder system.
---

# God Mode — Self-Improving Skill (Cursor)

Read `.claude/commands/improve.md` and execute that procedure exactly.

**Cursor-specific notes:**

- When editing `.cursor/rules/*.mdc` or `.cursor/skills/*/SKILL.md` files, use the IDE's built-in edit capability.
- When a pain point targets `fetchjobs.md`, also check whether the equivalent section in `jobsearch.mdc` needs the same fix (and vice versa). Propose both changes as separate proposals in sequence.
- When a pain point targets `setup.md`, do the same for `setup_from_resume.mdc`.
- For `SKILL_TOKEN_BLOAT` on a Cursor file that is a thin wrapper (i.e. delegates to a `.claude/commands/` file): skip — it is already minimal.
