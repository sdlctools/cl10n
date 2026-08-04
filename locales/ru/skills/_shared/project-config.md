# Project configuration reference

This file describes every variable used in `jira-sdlc-tools.env` and
`jira-sdlc-tools.local.env`. Both live in a **`.jst/` folder at the project
root** — `.jst/jira-sdlc-tools.env` and `.jst/jira-sdlc-tools.local.env` — and
that is the only location the skills read; a leftover copy at the root itself
is ignored. All project-specific values live in these two files — nothing else under `skills/`
should need editing after they're filled in.

Each skill's "Conventions used below" section names the tokens it needs
(e.g. `<PROJECT-KEY>`). Resolve them from the **healthcheck table** each
skill prints in its first step — `statuscheck` reports `PROJECT-KEY`,
`DEFAULT_BASE_BRANCH`, `PRODUCTION_BRANCH`, `WORKTREES_DIR` and
`JIRA_ACCOUNT_URL` as rows precisely so a run never has to open the files
to get them. The tables below describe what each variable means; read
*Reading config safely* before opening either file yourself.

## What lives in `.jst/`

| File | Purpose | Committed? |
| -- | -- | -- |
| `.jst/jira-sdlc-tools.env` | Team-shared settings (project key, status names, default branch). Same for every developer. | **Yes** — checked into the repo |
| `.jst/jira-sdlc-tools.local.env` | Developer/machine-specific settings (worktrees path, Jira URL, email, token path). Different per machine. | **No** — ignored by `.jst/.gitignore`, which lists it as `jira-sdlc-tools.local.env` |
| `.jst/.gitignore` | One line, `jira-sdlc-tools.local.env` — keeps the credential file out of git. It lives here rather than in the root `.gitignore` so it travels with any copy of `.jst/`, into a worktree for instance. | **Yes** — checked into the repo |
| `.jst/bootstrap.sh` / `.jst/bootstrap.ps1` | **Optional.** A script, not variables: turns a fresh worktree into a *runnable* instance. `jira-task-executor` runs it. Absence is normal — see below. | **Yes** — checked into the repo |

Both env files are sourced by tools that need them. Values in
`jira-sdlc-tools.local.env` override those in `jira-sdlc-tools.env` if both
define the same variable (though they define disjoint sets by convention).

### Reading config safely

The two files are **not equally sensitive**, and the split is uneven — which
is why "just read the config" is the wrong instinct:

| File | Contents | Reading it |
| -- | -- | -- |
| `jira-sdlc-tools.env` | `PROJECT_KEY`, `DEFAULT_BASE_BRANCH`, `PRODUCTION_BRANCH`, the four `STATUS_*` names | Committed and secret-free — read it whole, freely |
| `jira-sdlc-tools.local.env` | `WORKTREES_DIR`, `JIRA_ACCOUNT_URL`, `CONVERSATIONS_*` — **mixed in with** `JIRA_{ASSIGNER,EXECUTOR,REVIEWER}_TOKEN` and `GITHUB_PAT_TOKEN` | **Never dump it.** One `cat` puts three live Jira API tokens and a GitHub PAT into the session transcript |

Almost nothing needs the second file: `statuscheck`'s rows already carry
`PROJECT-KEY`, `WORKTREES_DIR`, `JIRA_ACCOUNT_URL`, `DEFAULT_BASE_BRANCH` and
`PRODUCTION_BRANCH`, so read the value off the row you already printed.

When a value genuinely has no row, read **that one key** — the file never has
to be opened whole:

```bash
grep -E '^[[:space:]]*JIRA_ACCOUNT_URL[[:space:]]*=' .jst/jira-sdlc-tools.local.env
```

⚠️ **Don't reach for a redaction filter instead.** The obvious one is worse
than useless: `sed 's/=.*TOKEN.*/=<redacted>/'` requires `TOKEN` to appear
*after* the `=`, but these lines read `JIRA_EXECUTOR_TOKEN=…` — the word is
before it. Nothing matches, the command still exits 0, and the tokens are in
the transcript with no error to notice. A filter that silently fails open is
exactly how this leaked once already; a single-key `grep` can't fail that way.

### `.jst/bootstrap.sh` / `.jst/bootstrap.ps1` — the optional worktree hook

The two env files above are required; this one is not, and **most projects
won't have it**. Nothing warns, blocks, or fails when it's missing — the
`bootstrap` healthcheck row reports either way as INFO, and the executor says
nothing about it.

Write one when a fresh worktree of your project isn't *runnable* until
something is provisioned for it: a cloned database, an instance index that
picks the ports, a compose stack scoped to the checkout, installed
dependencies. The plugin creates N worktrees; it can't know what your stack
needs to become N running instances, so this script is where your project
writes that down once instead of it living in one developer's head. To draft
one, start from the shipped example pair,
`plugins/jira-sdlc/docs/examples/bootstrap.example.sh` /
`bootstrap.example.ps1`.

It's **tracked**, unlike the gitignored `jira-sdlc-tools.local.env`, and that's
the point: a linked worktree is born with it, with no copy step to arrange (the
reason `ensure_local_env` exists for local.env). Ship `bootstrap.sh` on POSIX
projects and `bootstrap.ps1` on Windows ones — both if your team spans the two;
the OS dispatch rule is the same one every other script pair here uses, so each
side runs its own port and the `bootstrap` row names the one resolved for the
current OS.

**Who runs it, and when.** `jira-task-executor` does, in its step 1 — once per
worktree, at the moment someone is about to work in it. Deliberately *not* the
assigner: a multistep assigner run creates N worktrees in one unattended pass
and would stand up N running environments at once. It runs **automatically,
with no confirmation prompt** — that automatic execution is the whole point,
and it's a tracked, reviewed file in your own repo. `statuscheck` only reports
whether the file exists; it never executes it, so a hook's output lands in the
executor's transcript rather than being swallowed inside a reporting script.

**Fail-soft, always.** A non-zero exit is reported in the executor's output and
the run continues — a broken hook never blocks the executor, fails the
healthcheck, or aborts the issue. Provisioning is environment setup; the issue
is still workable without it.

**Idempotency is your script's job.** Re-invoking the executor in the same
worktree (a re-run after a rejected review, say) re-runs the hook, so write it
to tolerate running twice: create-if-missing rather than create, reuse an
existing container/volume rather than erroring on it.

**Environment contract.** The executor exports these before invoking, and both
ports use identical names:

| variable | value |
| -- | -- |
| `JST_ISSUE_KEY` | the issue key derived from the branch, e.g. `PROJ-402` |
| `JST_WORKTREE_DIR` | absolute path of *this* worktree's root (the script's working directory), not `WORKTREES_DIR` |
| `JST_BRANCH` | the current branch, e.g. `feature/PROJ-402-some-slug` |
| `JST_PARENT_BRANCH` | the PR base from `git config branch.<branch>.parentbranch`; empty when unset |
| `JST_PROJECT_KEY` | `<PROJECT-KEY>` from `jira-sdlc-tools.env` |

Env vars rather than positional arguments so the set can grow later without
breaking scripts already written against it — a script that ignores a variable
it doesn't know about keeps working. A project deriving **per-instance ports
should derive them deterministically from `JST_ISSUE_KEY`** (hash or parse the
numeric part into an index), so the same worktree gets the same ports on every
run and two worktrees can't collide.

The companion doc is [`docs/RUNNING-MULTIPLE-COPIES.md`](../../docs/RUNNING-MULTIPLE-COPIES.md):
that one is how to *decide* what each worktree's instance shares versus
isolates (database, cache, storage, queue, ports); this script is where your
project records the answer it landed on, in runnable form.

⚠️ Not to be confused with the **no-assigner provisioning** procedure in
[`jira-api-reference.md` §12](jira-api-reference.md) — that one creates a
branch and worktree for an issue the assigner never touched (git-level setup,
before the executor runs). This hook runs *inside* an existing worktree and
provisions the app's runtime around it.

Every skill's pre-flight healthcheck (`statuscheck`) has a `jst_dir` row that
FAILs — before any other check runs — when `.jst/` is missing, so a checkout
without it halts rather than running half-configured.

## Required (in `.jst/jira-sdlc-tools.env`)

| Token | What it is | Example |
| -- | -- | -- |
| `<PROJECT-KEY>` | Your Jira project key. | `PROJ` |
| `<DEFAULT_BASE_BRANCH>` | The branch new top-level work starts from when there's no parent context yet. | `development` |
| `<PRODUCTION_BRANCH>` | The production branch that hotfixes branch from and target. | `main` |
| `<STATUS_TODO>` | Status used for newly created issues. No skill reads or transitions to this value — it exists so it can be documented/offered as the default landing status, and so the optional `jira_issue_transition_on_branch.yml` GitHub Action (see `docs/STATE-TRANSITIONS-WITH-GITHUB-ACTIONS.md`) has a value to mirror into its hardcoded `SOURCE=` literal, since that workflow can't read this file. | `To Do` |
| `<STATUS_IN_PROGRESS>` | Status `jira-task-executor` transitions an issue to when it starts work. | `In Progress` |
| `<STATUS_IN_REVIEW>` | Status used when a PR is opened and under review. | `In Review` |
| `<STATUS_DONE>` | Final status reached when PRs are merged (typically by GitHub-for-Jira automation when a PR is merged into the base/parent branch). No skill transitions to this state on its own: `jira-task-reviewer` step 7 offers it for approved issues at the end of a run and moves only what you approve; otherwise it is handled by automation or a manual `jira.sh issue transition <KEY> --to "<STATUS_DONE>"`. Must match your workflow's real status name exactly. | `Done` |

## Required (in `.jst/jira-sdlc-tools.local.env`)

| Token | What it is | Example |
| -- | -- | -- |
| `<WORKTREES_DIR>` | Where per-issue worktrees are created. **Must be an absolute path** — a relative one resolves against a different base depending on where a skill runs (the main checkout for `jira-task-assigner`, a linked worktree for the other two), so statuscheck FAILs on it. A sibling of your repo is still the sensible place; just spell it out in full. Must already exist — `jira-task-assigner` will not create it. | `/home/you/src/myapp-worktrees` |
| `<JIRA_ACCOUNT_URL>` | Your Jira Cloud site URL (the `*.atlassian.net` domain). `jira.sh` uses it to resolve the cloud id (from `_edge/tenant_info`), and it constructs issue browse links (`https://<JIRA_ACCOUNT_URL>/browse/<KEY>`). | `your-site.atlassian.net` |

The three **role credential pairs** are required too — they're the whole of the
auth model, so they get their own section below.

### Auth — per-request, no login step

There is **no login and no keyring**. The `jira.sh` / `jira.ps1` client
(`skills/_shared/jira-api-reference.md` §9) authenticates **per-request**:
every call resolves its credential from these env files and sends it as Basic
auth on that one request. Nothing to set up before first use — fill in the
variables above plus the three role pairs below and the skills work.

Every token is the raw token value; a file path is not accepted. Create the
tokens at `id.atlassian.com` → Security → API tokens (classic or scoped both
work through the gateway — see `jira-api-reference.md` §5).

Each skill picks its role identity per-request with `jira.sh --role assigner|executor|reviewer`, which selects that role's pair below. `--role` is
**required** — there is no default account to fall back on. Because auth is
per-request, different roles can run **concurrently** as different identities
with no shared, machine-global "active account" to race over — the reason the
earlier login-based model was replaced.

## Required — per-role Jira accounts (in `.jst/jira-sdlc-tools.local.env`)

Each skill runs as its **own** Jira account, so the board shows who did
what: the assigner filed it, the executor implemented it, the reviewer
approved it. All six variables below are required — each role needs BOTH its
own email and its own token, and there is no default pair behind them. The
three accounts may of course be the same Atlassian account if you'd rather not
split them; state it explicitly in all three pairs.

| Token | What it is | Example |
| -- | -- | -- |
| `<JIRA_ASSIGNER_EMAIL>` / `<JIRA_ASSIGNER_TOKEN>` | The account `jira-task-assigner` runs as — it creates the issues and their comments. | `assigner@example.com` / `ATATT3xFfGF0…` |
| `<JIRA_EXECUTOR_EMAIL>` / `<JIRA_EXECUTOR_TOKEN>` | The account `jira-task-executor` runs as. Doubles as the **assignee**: the assigner puts this email on every issue it creates, and the executor refuses to work an issue that isn't assigned to it. | `executor@example.com` / `ATATT3xFfGF0…` |
| `<JIRA_REVIEWER_EMAIL>` / `<JIRA_REVIEWER_TOKEN>` | The account `jira-task-reviewer` runs as — it posts the verdict comments. | `reviewer@example.com` / `ATATT3xFfGF0…` |

Tokens are the raw API token **value**, never a path to a file. A role missing
either half is an error, not a fallback: `jira.sh --role <role>` stops and
names the variable to set, rather than quietly acting as somebody else.

**What this enables.** The assigner assigns every issue it creates
(top-level and sub-task) to the executor's email rather than leaving it
unassigned for board triage — the previous default. The executor then
**gates on ownership**: before any status transition or work, it refuses an
issue not assigned to the executor, prints the command to assign it, and
exits without transitioning, branching, committing, or commenting. The gate
holds against whichever account `JIRA_EXECUTOR_EMAIL` names — including the
case where all three roles point at one Atlassian account.

**It's in the scripts, not in skill prose.** All of them parse the env files
with the same `NAME = value` parser and local-overrides-team precedence as
`statuscheck.sh`, and the gate scripts are driven by their **exit code**. There
is no login script to run first — each call simply picks its role:

```bash
# jira-task-assigner — the address to put on the new issues' assignee, and nothing else:
ASSIGNEE_EMAIL=$(bash skills/_shared/scripts/posix/get_assignee_email.sh) || exit 1

# jira-task-executor — the issue must belong to the executor identity:
bash skills/_shared/scripts/posix/check_assignee.sh --role executor   # 0 = continue, non-zero = stop
```

`check_assignee.sh` resolves its own identity by calling `jira.sh --role <role> whoami` (the account that role's credential authenticates as) and
compares that account's `accountId` to the issue's assignee. `--role` is what
decides which identity is demanded — no ambient logged-in state to consult.
Unassigned, assigned to someone else, unreadable, or a hidden assignee email
are all the same answer: halt, with the `jira.sh issue assign …` command to fix
it on stderr.

Because auth is per-request, there is **no machine-global side effect** and
nothing to restore at the end of a run: the executor authenticating as the
executor for its own calls doesn't change what any other shell or a parallel
skill sees. This is the concrete payoff of the per-request model over the old
single-active-account login — parallel runs as distinct identities simply
can't race.

## Worked example

The README's usage walkthrough assumes these filled-in files:

**`.jst/jira-sdlc-tools.env` (committed):**

```
PROJECT-KEY           = PROJ
DEFAULT_BASE_BRANCH   = development
PRODUCTION_BRANCH     = main
STATUS_TODO           = To Do
STATUS_IN_PROGRESS    = In Progress
STATUS_IN_REVIEW      = In Review
STATUS_DONE           = Done
```

**`.jst/jira-sdlc-tools.local.env` (gitignored):**

```
WORKTREES_DIR         = /home/you/src/myapp-worktrees
JIRA_ACCOUNT_URL      = your-site.atlassian.net
# All three role pairs are required — one email + one token each, no default:
JIRA_ASSIGNER_EMAIL   = assigner@example.com
JIRA_ASSIGNER_TOKEN   = ATATT3xFfGF0…
JIRA_EXECUTOR_EMAIL   = executor@example.com
JIRA_EXECUTOR_TOKEN   = ATATT3xFfGF0…
JIRA_REVIEWER_EMAIL   = reviewer@example.com
JIRA_REVIEWER_TOKEN   = ATATT3xFfGF0…
```
