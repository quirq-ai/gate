# Agent guide

How an agent changes this repo safely. Read `README.md` first.

- Every change is a pull request against `main`, titled with its work item id (for example
  `V0-GAT-01: ...`). It lands only with the `presubmit` check green.
- The gate is a core repo: its code names no language, build tool or deploy target. Repo-specific
  facts come from infra-config and manifests. GitHub-specific code sits behind the `backend`
  field, in its own module, so `launchpad` (quirq's own cloud) slots in later.
- Other qq repos are used by pinned commit, never copied.
- Policy (what is required, who may approve) lives in infra-config, not here, and changing it
  needs suraj. No agent can override the gate.
- `.github/CODEOWNERS` names suraj (`@sharmasuraj0123`) as owner of the policy and trust paths,
  including the code privileged workflows run; owner names are his call, so never change them. Leave any other `owners` list empty.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- This repo is public: no secrets, tokens or internal hostnames.
