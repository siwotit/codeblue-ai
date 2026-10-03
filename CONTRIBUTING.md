# Contributing to CodeBlue AI

CodeBlue AI is a Claude Code plugin. Users install it from a marketplace.

## What you should do

1. Set up the repo and a read-only AWS profile.
2. Make one focused change (a skill, the agent, or the MCP config).
3. Validate the plugin.
4. Test the change against a real cluster with describe-only calls.
5. Bump the version and open a pull request.

## 1. Set up

You need:
- Claude Code
- `uv` (the MCP servers start with `uvx`)
- AWS CLI v2.32 or later, signed in
- A **read-only** AWS profile.

Check the profile works:

```
aws sts get-caller-identity --profile <your-read-only-profile>
```

Run the plugin from your working copy, from any directory:

```
claude --plugin-dir /path/to/codeblue-ai --agent codeblue-ai
```

Set your profile and region in the plugin options (`claude plugin configure codeblue-ai`).

## 2. Know where things live

| Path | What it is | Change it when |
|------|------------|----------------|
| `agents/codeblue-ai.md` | The agent: persona, read-only rules, reporting format, escalation guidance | Behavior that applies to every investigation changes |
| `skills/eks/SKILL.md` | Primary skill for EKS investigation | EKS investigation logic changes |
| `skills/ec2/SKILL.md` | Instance-level investigation | Instance checks change |
| `skills/cloudwatch/SKILL.md` | Metrics, alarms and log queries | Metric or log handling changes |
| `skills/*/examples/` | One worked example per skill | A skill's output format changes |
| `.mcp.json` | MCP servers | A server is added, removed or reconfigured |
| `.claude-plugin/plugin.json` | Plugin manifest and install options | Version, metadata or options change |
| `.claude-plugin/marketplace.json` | Lets the repo be added as a marketplace | Version changes |

A root `CLAUDE.md` is not loaded from plugins, so do not put agent behavior there. Put it in `agents/codeblue-ai.md` or a skill.

## 3. Make a change

### Edit an existing skill

1. Open the `SKILL.md`. Keep the frontmatter `name` and `description`. The description decides when the skill is used, so update it if the scope changes.
2. Edit the decision tree branch, or add a new branch in the same format.
3. Use read-only calls only (describe, list, get, lookup).
4. If the skill hands over to another skill, name it and say what context to pass (resource IDs, cluster, time window, the question).
5. If the output format changed, update the skill's `examples/investigation-output.md`.

### Add a skill

1. Create `skills/<name>/SKILL.md` with frontmatter:
   ```
   ---
   name: <name>
   description: <what it investigates and when to use it>
   ---
   ```
2. Write the decision tree and the "What to Report" section, following `skills/ec2/SKILL.md`.
3. Add the handover from `eks` (or the relevant skill) and from the skill back.
4. Add an `examples/investigation-output.md`.

Skills install together and hand over to each other by name (for example, `eks` hands over to `ec2`). Only refer to skills and tools that this plugin includes.

### Change the agent

1. Edit `agents/codeblue-ai.md`.
2. Keep the read-only rule and the "establish context first" rule.
3. Do not add tools that can write.

### Change MCP servers or install options

1. Edit `.mcp.json`. Use `${user_config.<option>}` for anything a user might need to change.
2. To add an option, declare it under `userConfig` in `.claude-plugin/plugin.json` with a title, description and default.
3. Do not hardcode profile names, account IDs or regions.

## 4. Validate

Run from the repo root:

```
claude plugin validate . --strict
claude plugin validate agents --strict
claude plugin validate skills --strict
```

All three must pass.

## 5. Test

1. Start the agent as shown in step 1.
2. Ask a question your change affects, using a real cluster in a non-production account.
3. Confirm:
   - It reaches the branch you changed.
   - It makes describe, list and get calls only.
   - It states what it could not check.
4. Ask for a write action (for example, " add a billing tag to that instance") and confirm it refuses.

## 6. Release and pull request

1. Bump `version` in `.claude-plugin/plugin.json` and in `.claude-plugin/marketplace.json`. Both must match.
2. Commit with a message that says what changed and why.
3. Open a pull request. In the description, list which scenarios you tested.
4. After merge, users update with `/plugin update codeblue-ai`.

