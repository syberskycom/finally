# Review

## Findings

1. **High: Enabling the plugin makes every Claude stop run an unbounded review command.**  
   `independent-reviewer/.claude-plugin/hooks/hooks.json:3` registers a global `Stop` hook with no matcher or guard, and `independent-reviewer/.claude-plugin/hooks/hooks.json:8` runs `codex exec "Review changes since last commit and write results to a file named planning/REVIEW.md"`. Once `.claude/settings.json:6` enables this plugin, every Claude response stop in the project will launch a separate Codex process and rewrite `planning/REVIEW.md`, even for unrelated prompts. That can make normal sessions unexpectedly slow, mutate the working tree after ordinary interactions, and repeatedly review/generated-review changes instead of only the intended code changes. Consider making this a slash command or explicit agent action, or add a narrow guard so the hook only fires for the intended workflow.

2. **Medium: The packaged plugin does not include the review agent or command files that define the intended user-facing behavior.**  
   `.claude-plugin/marketplace.json:10` points the install source at `./independent-reviewer`, but that directory only contains `independent-reviewer/.claude-plugin/plugin.json` and `independent-reviewer/.claude-plugin/hooks/hooks.json`. The new agent and command files live outside the plugin source at `.claude/agents/reviewer.md`, `.claude/agents/change-reviewer.md`, and `.claude/commands/doc-review.md`, so installing `independent-reviewer@sybersky-tools` from the marketplace would not install those capabilities. A user would get only the Stop hook, not the documented `change-reviewer`/`reviewer` behavior. Move the agent/command definitions into the plugin package if they are part of the plugin, or remove them from this change if they are meant to be project-local only.

3. **Low: Removing the project README drops the only top-level quick start and environment documentation.**  
   `README.md` is deleted entirely. The remaining `planning/PLAN.md` is a design/spec document, but it is not a substitute for the quick-start instructions, environment variable table, architecture summary, and project structure that were previously available at the repository root. If this deletion was intentional, consider replacing it with a shorter README that points users to the current setup path and relevant planning docs.

## Validation

- Ran `jq empty` on `.claude-plugin/marketplace.json`, `.claude/settings.json`, `independent-reviewer/.claude-plugin/plugin.json`, and `independent-reviewer/.claude-plugin/hooks/hooks.json`; all JSON files parse successfully.
