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
| MCP `aws-mcp` | AWS MCP Server through the SigV4 proxy, started with `--read-only` |
| MCP `awslabs.cloudwatch-mcp-server` | Metric and log analysis |

The skills install together and call on each other during an investigation. `eks` is the usual starting point.

## Prerequisites

1. [`uv`](https://docs.astral.sh/uv/) installed (the MCP servers start with `uvx`).
2. AWS CLI v2.32+ signed in (`aws login` or `aws configure sso`).
3. **A read-only AWS profile.** The agent is read-only by design, but the profile is the real safety net. Attach a read-only policy such as `ReadOnlyAccess`, plus the permissions the AWS MCP Server itself requires (see [AWS MCP Server managed policies](https://docs.aws.amazon.com/aws-mcp/latest/userguide/security-iam-awsmanpol.html)). Do not point it at an admin profile.
4. Check it works: `aws sts get-caller-identity --profile <your-profile>`.

## Install

```
claude plugin marketplace add siwotit/codeblue-ai
claude plugin install codeblue-ai@codeblue-ai
```

The first command adds this repo as a marketplace (a catalog of plugins). The second installs the plugin from it. You do this once per machine. Inside a Claude Code session, the same commands work as `/plugin marketplace add ...` and `/plugin install ...`.

Claude Code asks for three options at install time (change later with `claude plugin configure codeblue-ai`):

| Option | Default | Meaning |
|--------|---------|---------|
| `aws_profile` | `codeblue-aiagent` | AWS CLI profile to use. Must be read-only |
| `aws_mcp_region` | `eu-central-1` | Region of the AWS MCP Server endpoint. Must be a [supported endpoint region](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html#step-2-choose-auth-method) |
| `aws_region` | `eu-central-1` | Default region for your resources (where your clusters run) |

The two regions are unrelated. `aws_mcp_region` is where the AWS MCP Server you connect to runs. `aws_region` is where your clusters are, and it is only a default: name another region in your question, such as "check us-west-2", and the agent uses that instead.

### Scripted setup

Skip the prompts by passing the options on install with `--config` (repeat for each option):

```
claude plugin install codeblue-ai@codeblue-ai --config aws_profile=my-readonly --config aws_region=us-west-2
```

## Use

Start the agent:

```
claude --agent codeblue-ai
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
