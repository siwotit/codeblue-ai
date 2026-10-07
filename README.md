# CodeBlue AI

A read-only diagnostic agent for Amazon EKS, packaged as a Claude Code plugin. Ask it why something is broken; it investigates through AWS APIs and reports what it found, with evidence. No kubeconfig or kubectl needed.

**Scope:** EKS at the AWS and compute layer: nodes and nodegroups (join failures, terminations, scale-up, NotReady under CPU/EBS pressure), control plane, access entries and IAM, managed addons, EBS volume problems. Pods, workloads and ingress are not covered yet; when the evidence points there, the agent hands you the exact `kubectl` commands.

## What's in the plugin

| Component | Role |
|-----------|------|
| Agent `codeblue-ai` | The persona, read-only rules, reporting format |
| Skill `eks` | EKS investigation: nodes, control plane, access, addons, EBS volumes |
| Skill `ec2` | Instance health: status checks, console output, EBS and CPU throttling |
| Skill `cloudwatch` | Metrics, baselines, alarms and logs |
| MCP `aws-mcp` | AWS MCP Server through the SigV4 proxy. Access is limited by your read-only AWS profile |
| MCP `awslabs.cloudwatch-mcp-server` | Metric and log analysis |

The skills install together and call on each other during an investigation. `eks` is the usual starting point.

## Prerequisites

1. **Claude Code** installed — it runs the plugin and provides the `claude` command.

   ```
   curl -fsSL https://claude.ai/install.sh | bash
   ```

   (Or see the [Claude Code setup guide](https://docs.claude.com/en/docs/claude-code/setup) for other install methods.) Check it works: `claude --version`.

2. **[`uv`](https://docs.astral.sh/uv/)** installed — the MCP servers start with `uvx`.

   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Check it works: `uvx --version`.

3. **AWS CLI** signed in (`aws login` or `aws configure sso`).

4. **A read-only AWS profile.** The agent is read-only by design, but the profile is the real safety net. Attach a read-only policy such as `ReadOnlyAccess`, plus the permissions the AWS MCP Server itself requires (see [AWS MCP Server managed policies](https://docs.aws.amazon.com/aws-mcp/latest/userguide/security-iam-awsmanpol.html)). Do not point it at an admin profile.

5. Check it works: `aws sts get-caller-identity --profile <your-profile>`.

## Install

```
claude plugin marketplace add siwotit/codeblue-ai
claude plugin install codeblue-ai@codeblue-ai
```

The first command adds this repo as a marketplace (a catalog of plugins). The second installs the plugin from it. You do this once per machine. Inside a Claude Code session, the same commands work as `/plugin marketplace add ...` and `/plugin install ...`.

Claude Code asks for two options at install time (change later with `claude plugin configure codeblue-ai@codeblue-ai`):

| Option | Default | Meaning |
|--------|---------|---------|
| `aws_profile` | `codeblue-aiagent` | AWS CLI profile to use. Must be read-only |
| `aws_mcp_region` | `eu-central-1` | Region of the AWS MCP Server endpoint. Must be a [supported endpoint region](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html#step-2-choose-auth-method) |

The agent does not have a default region. Name the region in your question, or it will ask which one to use.

### Scripted setup

Skip the prompts by passing the options on install with `--config` (repeat for each option):

```
claude plugin install codeblue-ai@codeblue-ai --config aws_profile=my-readonly --config aws_mcp_region=eu-central-1
```

## Use

Start the agent:

```
claude --agent codeblue-ai
```

The AWS profile and the AWS MCP Server region are plugin options. To change them, run this inside Claude Code and restart:

```
/plugin configure codeblue-ai@codeblue-ai
```

Then describe the problem, for example:

> Nodes in `prod-cluster` (eu-central-1) keep getting replaced and never join. Started around 09:00 UTC.

> One node went NotReady at 14:20 while CPU was high. Why?

## Principles

- **Read-only.** It never modifies, restarts, or deletes anything.
- **Evidence over assumptions.** Facts, reasoning, and unknowns are kept distinct.
- **Says what it couldn't check**, and what to do next.
- **Knows when to hand off:** to another skill, to you with kubectl, or to an AWS Support case with the details to include.

## Roadmap

- [x] EKS AWS/compute layer (nodes, control plane, access, addons, EBS)
- [ ] EKS Kubernetes layer: pods, workloads, ingress (needs cluster access)
- [ ] Other services (ECS, RDS, Lambda)

See `CONTRIBUTING.md` to work on the plugin.
