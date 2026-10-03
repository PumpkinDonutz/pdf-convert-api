# Toolchain setup: local dev → Azure → live

This is a step-by-step plan for standing up the full pipeline: you write/approve code locally with Claude Code, it gets pushed to a git remote, and from there it's built and deployed to Azure Container Apps (ACA) automatically. It's split into **things only you can do** (account creation, identity/consent steps that require your own login) and **things Claude can do for you afterward** (CLI setup, resource provisioning, workflow files) — so you know exactly what to hand off once each manual step is done.

The end state: `git push` to your main branch → GitHub Actions builds the Docker image → pushes it to Azure Container Registry (ACR) → deploys a new revision to ACA. No manual `az` commands or portal clicks needed for routine changes after setup.

## 0. What you need to create yourself (can't be delegated)

These require your own identity/credentials and a browser login, so Claude can't do them for you even with CLI access:

1. **An Azure account** with an active subscription (you mentioned you'll do this).
2. **A GitHub account** and a new (or existing) repository for this project, if you don't already have one.
3. **Azure CLI login consent** — the first `az login` on your machine opens a browser for you to authenticate. After that, a session/token exists that tools (including Claude via your terminal) can reuse until it expires.
4. **GitHub repository admin access** — needed once, to let Claude (via `gh` CLI, already authenticated as you) add secrets/variables and create the Actions workflow file via a PR or direct push, depending on how you want to review changes.

Once those four exist, everything below can be done by Claude running CLI commands in your terminal, with you reviewing each step before anything destructive or billable happens (per our working agreement — no resource creation, deployment, or secret changes without you seeing the command first).

## 1. Local developer toolchain

Install once, locally:

| Tool | Purpose |
| --- | --- |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) (with WSL2 backend on Windows) | Build and smoke-test the container locally before it ever reaches Azure. |
| [Azure CLI (`az`)](https://learn.microsoft.com/cli/azure/install-azure-cli) | Provision ACR, ACA, secrets, and budgets from the terminal. |
| [GitHub CLI (`gh`)](https://cli.github.com/) | Create the repo (if needed), set Actions secrets, open PRs, from the terminal. |
| Git | Already required; confirm `git --version` works. |
| Python 3.12 | For running the app and test suite outside Docker during day-to-day development. |

After installing `az` and `gh`, run:

```bash
az login
gh auth login
```

Both open a browser once; afterward Claude can run authenticated `az`/`gh` commands in this terminal without you re-authenticating each time (until the session token expires, at which point you'll be asked to `az login` again).

## 2. What Claude can set up after you hand off

Once the accounts above exist and you're logged in via `az` and `gh`, this is the sequence Claude can run — each step is a small number of reviewable CLI commands, not a black box:

### 2.1 Resource group and registry

- Create an Azure resource group in your preferred region.
- Create an Azure Container Registry (ACR) in that group.
- Enable an ACA-to-ACR managed identity pull (no registry password stored anywhere), as the design doc specifies.

### 2.2 Container Apps environment and app

- Create the ACA environment.
- Create the container app itself using the shape already defined in `DESIGN_AND_BUILD_INSTRUCTIONS_v2.md` section 7 (`minReplicas: 0`, `maxReplicas: 3`, `concurrentRequests: 1`, resource limits, env vars).
- Add `API_TOKENS` as an ACA secret (never as a build arg or baked into the image).

### 2.3 CI/CD wiring (GitHub Actions)

This is the piece that makes `git push` actually deploy. Recommended approach: **OpenID Connect (OIDC) federated credential** between GitHub Actions and Azure, instead of a long-lived service principal secret sitting in GitHub. Claude can:

- Create an Azure AD app registration + federated credential scoped to this repo and branch.
- Grant that identity just enough role (e.g. `AcrPush` on the registry, `Container Apps Contributor` on the resource group) — not subscription-wide `Owner`.
- Add the resulting IDs (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`) as GitHub Actions secrets via `gh secret set`.
- Write a `.github/workflows/deploy.yml` that, on push to `main`:
  1. Builds the Docker image.
  2. Logs in to Azure via OIDC (no stored password).
  3. Pushes the image to ACR with an immutable tag (e.g. the git SHA).
  4. Runs `az containerapp update` to point the app at the new image tag.

No Azure credentials ever live in the repo itself — only the three non-secret IDs above, which identify the federated trust relationship (not a password).

### 2.4 Budget and safety rails

- Set an Azure spending budget and alert threshold on the resource group, per the design doc's cost warning.
- Confirm `CONVERSION_TIMEOUT_SECONDS`, `MAX_PAGES`, and `API_TOKENS` are all set as intended before the first live smoke test.

## 3. Day-to-day workflow once this is set up

1. You and Claude make changes locally, run tests, and smoke-test in Docker.
2. Commit and push to a feature branch; open a PR (Claude can do this via `gh pr create`).
3. Merge to `main`.
4. GitHub Actions builds, pushes, and deploys automatically — no manual Azure steps.
5. Rotate API tokens by adding a new token to the ACA secret, deploying, switching callers, then removing the old token (same process as in the design doc, just triggered by a normal push instead of a manual `az` command).

## 4. What Claude will always check with you first

Per our standing agreement on risky/billable actions, Claude will pause for your confirmation before:

- Creating any Azure resource that incurs cost (registry, container app, budget).
- Granting any identity a role or permission.
- Pushing to `main`, merging a PR, or triggering a deployment.
- Rotating or removing an API token that active callers depend on.

Routine, reversible steps (reading current resource state, running local tests, drafting the workflow YAML for your review) don't need a check-in each time.

## 5. Order of operations (checklist)

- [ ] You create the Azure account and subscription.
- [ ] You create/confirm the GitHub repository.
- [ ] You run `az login` and `gh auth login` locally, once.
- [ ] Claude creates the resource group, ACR, and ACA environment (you approve each command).
- [ ] Claude sets up the OIDC federated credential and GitHub Actions secrets.
- [ ] Claude writes `.github/workflows/deploy.yml` and the initial ACA container app definition.
- [ ] You approve a budget + alert.
- [ ] First push to `main` triggers the first live deployment; run the smoke tests from `DESIGN_AND_BUILD_INSTRUCTIONS_v2.md` section 10 against the live URL.
