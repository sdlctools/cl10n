# jira-api-reference.md (Jira Cloud REST v3 — the `jira.sh` client + the raw API)

The single operational reference for Claude Code driving Jira. The three
skills (`jira-task-assigner`, `jira-task-executor`, `jira-task-reviewer`)
and `_shared/scripts` drive Jira through **`jira.sh`** (POSIX) / **`jira.ps1`**
(Windows) — a small REST v3 client that wraps the raw calls documented here.
This file is both halves of that:

- **§9–§13 — the operational surface the skills invoke**: the `jira.sh`
  command surface + `--role` auth (§9), issue field lists (§10), comment
  mechanics & markers (§11), the git branch convention (§12), and the
  PR-base resolver (§13). Start here for anything a skill does.
- **§0–§8 — the raw REST substrate**: the exact `curl` shapes `jira.sh`
  implements, and the direct-REST path for callers that can't use it — a
  GitHub Actions runner, or a **scoped** API token. Every `curl` below is a
  *verified working call*, run against a live Jira Cloud instance and the
  exact shapes the `.github/workflows/jira_issue_transition_*.yml` workflows
  use.

Project-specific values are `<TOKEN>`s resolved from the two config files in
the project root's **`.jst/` folder** — the only location read
(see [`project-config.md`](project-config.md)):

**`.jst/jira-sdlc-tools.local.env` (machine-specific, gitignored)**
- `<JIRA_ACCOUNT_URL>` — e.g. `your-site.atlassian.net` (a scheme is tolerated; it gets stripped)
- three **required** role pairs `JIRA_{ASSIGNER,EXECUTOR,REVIEWER}_{EMAIL,TOKEN}` — an account + its API token (classic **or** scoped; see §5) per role, selected by `jira.sh --role` (§9). Each role needs BOTH its own email and its own token; there is no default account and no fallback

`<CLOUD_ID>` is *not* configured — resolve it at runtime (see §1).

**Sections:** [0. The one rule that matters](#0-the-one-rule-that-matters-host--auth) ·
[1. Resolve the cloud id](#1-resolve-the-cloud-id) ·
[2. A reusable auth helper](#2-a-reusable-auth-helper) ·
[3. Read an issue's status](#3-read-an-issues-status) ·
[4. Transition an issue by status name](#4-transition-an-issue-by-status-name) ·
[5. Token types & scopes](#5-token-types--scopes) ·
[6. People fields — assignee and reporter](#6-people-fields--assignee-and-reporter) ·
[7. Bulk field updates across a project](#7-bulk-field-updates-across-a-project) ·
[8. Gotchas](#8-gotchas) ·
[9. The `jira.sh` client — command surface & `--role` auth](#9-the-jirash-client--command-surface--role-auth) ·
[10. Issue field lists](#10-issue-field-lists) ·
[11. Comments & markers](#11-comments--markers) ·
[12. Git branch convention](#12-git-branch-convention) ·
[13. PR-base resolver](#13-pr-base-resolver)

---

## 0. The one rule that matters: host + auth

Jira Cloud is reachable two ways, and **which host you use decides whether a
scoped token works**:

| Host | Auth | Classic token | Scoped token |
|---|---|---|---|
| `https://<JIRA_ACCOUNT_URL>` (site domain) | Basic (`-u email:token`) | ✅ | ❌ `401 AUTHENTICATED_FAILED` |
| `https://api.atlassian.com/ex/jira/<CLOUD_ID>` (gateway) | Basic (`-u email:token`) | ✅ | ✅ |

Bearer auth (`Authorization: Bearer <token>`) does **not** work with either
kind of API token here (that's for real OAuth 3LO access tokens) — use
Basic.

**Therefore: always go through the `api.atlassian.com/ex/jira/<CLOUD_ID>`
gateway.** It works for both token types, so it's the portable choice —
`jira.sh` (§9) hits the gateway for exactly this reason, which is what lets a
scoped token work through it.

## 1. Resolve the cloud id

The gateway is addressed by cloud id, not host name. The site's
`_edge/tenant_info` endpoint returns it and needs **no auth**:

```bash
SITE="${JIRA_ACCOUNT_URL#*://}"                 # strip any scheme
CLOUD_ID=$(curl -fsSL "https://$SITE/_edge/tenant_info" | jq -r '.cloudId')
BASE="https://api.atlassian.com/ex/jira/$CLOUD_ID/rest/api/3"
```

```json
// GET https://<JIRA_ACCOUNT_URL>/_edge/tenant_info   →  200
{ "cloudId": "00000000-0000-0000-0000-000000000000" }
```

## 2. A reusable auth helper

Every authenticated call is Basic auth against `$BASE`. Define once:

```bash
api() { curl -sSfL -u "$JIRA_EXECUTOR_EMAIL:$JIRA_EXECUTOR_TOKEN" -H "Accept: application/json" "$@"; }
```

(Any of the three role pairs works — pick the one whose identity the call
should carry. `jira.sh` does exactly this per request, §9.)

`-sSfL` = silent, but show errors, **fail the process on any HTTP ≥ 400**
(so `set -euo pipefail` catches auth/scope problems), and follow redirects.

Confirm the token authenticates at all:

```bash
api "$BASE/myself" | jq -r '.displayName'        # → "Ada Lovelace"   (200)
```

## 3. Read an issue's status

```bash
api "$BASE/issue/<KEY>?fields=status" | jq -r '.fields.status.name'
```

```json
// GET $BASE/issue/<KEY>?fields=status   →  200
{
  "key": "<KEY>",
  "fields": { "status": { "name": "<STATUS_TODO>" } }
}
```

## 4. Transition an issue by status name

The REST API transitions by **transition id**, not by target status name.
So it's two calls: list the available transitions, pick the one whose
**destination status** matches the name you want, then POST its id.

**4a. List available transitions** (depends on the issue's *current* status):

```bash
api "$BASE/issue/<KEY>/transitions"
```

```json
// GET $BASE/issue/<KEY>/transitions   →  200
{
  "transitions": [
    { "id": "11", "name": "To Do",       "to": { "name": "<STATUS_TODO>" } },
    { "id": "21", "name": "In Progress", "to": { "name": "<STATUS_IN_PROGRESS>" } },
    { "id": "31", "name": "In Review",   "to": { "name": "<STATUS_IN_REVIEW>" } },
    { "id": "41", "name": "Done",        "to": { "name": "<STATUS_DONE>" } }
  ]
}
```

**4b. Resolve the target status name → transition id** (`.to.name`, not
`.name`):

```bash
TID=$(api "$BASE/issue/<KEY>/transitions" \
  | jq -r --arg t "<STATUS_IN_PROGRESS>" 'first(.transitions[] | select(.to.name == $t) | .id) // empty')
# empty TID ⇒ no transition to that status is available from the current one
```

**4c. Perform the transition** (returns `204 No Content` on success):

```bash
api -X POST -H "Content-Type: application/json" \
  -d "{\"transition\":{\"id\":\"$TID\"}}" \
  "$BASE/issue/<KEY>/transitions"
```

Full round-trip that was verified (`<STATUS_TODO>` → `<STATUS_IN_PROGRESS>`
→ back), all `204`:

```
status before   : <STATUS_TODO>
POST transition (id 21)  →  204   →  status: <STATUS_IN_PROGRESS>
POST transition (id 11)  →  204   →  status: <STATUS_TODO>
```

## 5. Token types & scopes

Create tokens at `id.atlassian.com` → Security → API tokens. Two kinds come out
of that page, and they don't behave the same here:

- **Classic, unscoped** ("Create API token") — works on both hosts (§0); carries
  the account's full Jira permissions. Simplest; restrict via the account's
  project permissions.
- **Scoped** ("Create API token with scopes") — least privilege, **gateway
  only**. Its scopes come in two flavours: **coarse** (three scopes, works) and
  **granular** per-resource (a documented trap — see below the table).

Each skill authenticates as its own role (§9), so a token only ever needs to
cover that role's own calls:

- **assigner** — `whoami`, `project exists`, `issue create` (which resolves
  `--assignee` through a user search), `comment add`, `issue delete`
- **executor** — `whoami`, `project exists`, `issue view`, `issue transition`,
  `comment add` / `comment list`
- **reviewer** — `whoami`, `project exists`, `issue view`, `comment add` /
  `comment list`, `issue transition`

What each token kind grants that role:

| Role | Non-scoped | Scoped classic (coarse) | Scoped granular |
|---|---|---|---|
| **assigner** | N/A | `read:jira-user`, `read:jira-work`, `write:jira-work` | `read:user:jira`, `read:project:jira`, `read:field:jira`, `read:issue-type:jira`, `write:issue:jira`, `write:comment:jira`, `delete:issue:jira` — **plus** whatever else `POST /issue` demands (same bundle problem) |
| **executor** | N/A | `read:jira-user`, `read:jira-work`, `write:jira-work` | `read:user:jira`, `read:project:jira`, `read:issue:jira`, `read:issue.transition:jira`, `read:comment:jira`, `write:issue:jira`, `write:comment:jira` — **plus** the whole `GET /issue` read bundle |
| **reviewer** | N/A | `read:jira-user`, `read:jira-work`, `write:jira-work` | `read:user:jira`, `read:project:jira`, `read:issue:jira`, `read:issue.transition:jira`, `read:comment:jira`, `write:issue:jira`, `write:comment:jira` — **plus** the whole `GET /issue` read bundle |

**Why Non-scoped is N/A in every row**: an unscoped classic token carries the
account's full Jira permissions, so there is no per-role scope list to give —
you narrow it by narrowing the *account's* project permissions instead, not by
picking scopes.

**The three coarse scopes**, which are identical for all three roles because
every role reads issues and writes something back:

| Scope | Grants |
|---|---|
| `read:jira-user` | `GET /myself` (identity), `GET /user/search` (email → accountId) |
| `read:jira-work` | `GET /issue`, `GET /issue/{key}/transitions`, `GET /issue/{key}/comment`, `GET /project/search` |
| `write:jira-work` | `POST /issue`, `POST /issue/{key}/transitions`, `POST /issue/{key}/comment`, `PUT /issue/{key}/assignee`, `DELETE /issue/{key}` |

⚠️ **The granular column documents a trap — it is not a recommendation.** The
per-resource scopes are listed above because they're what you'd reach for, not
because that configuration is known to work: `GET /issue` requires a whole
bundle of granular read scopes *simultaneously* (`read:issue-meta:jira`,
`read:issue-security-level:jira`, `read:issue.vote:jira`,
`read:issue.changelog:jira`, `read:avatar:jira`, `read:status:jira`,
`read:attachment:jira`, `read:issue-link:jira`, `read:priority:jira`, … — the
list is long and undocumented per-endpoint), and **any missing member returns
`401 {"message":"Unauthorized; scope does not match"}`** rather than naming what
it wanted. Each row's granular list is therefore a starting point that will be
incomplete. **Use the three coarse scopes** — they cover every call in the
per-role lists above and avoid the bundle problem entirely.

## 6. People fields — `assignee` and `reporter`

`jira.sh` can set an **assignee** (`issue assign`, or `issue create --assignee`)
but exposes **no reporter operation** — reporter is REST-only, which is the main
reason a skill would reach past `jira.sh` to the raw calls below.

The two fields differ in a way worth knowing before you plan a change:

| field | mutable? | notes |
|---|---|---|
| `assignee` | yes | who *works* it. `jira.sh issue assign` does this — prefer it. |
| `reporter` | yes, **with permission** | who *filed* it. Needs the project's **Modify Reporter** permission (admin-level by default). |
| `creator` | **never** | set by Jira from the authenticated caller at create time. No API can change it, ever. |

**`creator` and `reporter` are set from whoever authenticates on create** — so
the ordinary way to get them right is simply to *be the right account*, which
under per-request auth means creating with `jira.sh --role assigner` (§9); no
field-setting needed. Reach for the REST writes below only to **retrofit
existing issues**.

### Check it's writable before you try

`editmeta` reports exactly which fields this account may set on this issue.
Do this first — it turns a permission problem into an answer instead of a
failed write:

```bash
api "$BASE/issue/<KEY>/editmeta" | jq '.fields | keys'          # what I may edit
api "$BASE/issue/<KEY>/editmeta" | jq '.fields.reporter.operations'
```

```json
// → ["set"]   ⇒ Modify Reporter is granted; the PUT below will work.
// reporter absent from .fields entirely ⇒ not permitted. Stop; ask an admin.
```

### Set them (both in one PUT)

People fields take an **`accountId`**, not an email. Resolve it first:

```bash
# email → accountId
api --get --data-urlencode "query=<EMAIL>" "$BASE/user/search" | jq -r '.[0].accountId'
# → "<ACCOUNT_ID>"   (an opaque string — treat it as such; do not parse it)
```

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT \
  -u "$JIRA_EXECUTOR_EMAIL:$JIRA_EXECUTOR_TOKEN" -H 'Content-Type: application/json' \
  --data '{"fields":{
             "assignee": {"accountId":"<EXECUTOR_ACCOUNT_ID>"},
             "reporter": {"accountId":"<ASSIGNER_ACCOUNT_ID>"}
          }}' \
  "$BASE/issue/<KEY>"
# → 204, empty body. Success is the status code; there is nothing to parse.
```

## 7. Bulk field updates across a project

Retrofitting a whole project (e.g. making every existing issue look as if the
per-role identities had always been in place) is a loop of the §6 PUT. The
shape below is the one that matters — **collect keys → write → verify from the
server**.

### Collect the keys — paginate over REST search

`jira.sh` deliberately exposes no bulk-search subcommand (this whole section is
a rare human-run retrofit, not skill surface) — drive it with the raw call, or
`jira.sh raw GET /search/jql`. REST search pages with `nextPageToken` (*not*
`startAt` — that's the old `/search` endpoint):

```bash
next=""; : > keys.txt
while :; do
  resp=$(api --get \
    --data-urlencode 'jql=project = <PROJECT-KEY> ORDER BY key ASC' \
    --data-urlencode 'maxResults=100' --data-urlencode 'fields=key' \
    ${next:+--data-urlencode "nextPageToken=$next"} \
    "$BASE/search/jql")
  jq -r '.issues[].key' <<<"$resp" >> keys.txt
  next=$(jq -r '.nextPageToken // empty' <<<"$resp")
  [ -z "$next" ] && break
done
wc -l < keys.txt      # sanity-check the count BEFORE writing anything
```

### Write, counting failures rather than aborting

```bash
ok=0; fail=0
while read -r K; do
  code=$(curl -sS -o /tmp/err -w '%{http_code}' -X PUT \
    -u "$JIRA_EXECUTOR_EMAIL:$JIRA_EXECUTOR_TOKEN" -H 'Content-Type: application/json' \
    --data "{\"fields\":{\"assignee\":{\"accountId\":\"$EXE_ID\"},
                         \"reporter\":{\"accountId\":\"$ASG_ID\"}}}" \
    "$BASE/issue/$K")
  if [ "$code" = 204 ]; then ok=$((ok+1))
  else fail=$((fail+1)); echo "$K HTTP$code $(cat /tmp/err)" >&2; fi
done < keys.txt
echo "updated: $ok   failed: $fail"
```

A partial failure is normal (one issue in a screen you can't edit) and
shouldn't abort the other 78 — so count and report rather than `set -e`.

### Verify from the server, not from the exit codes

A `204` means the request was accepted, not that the project now looks how you
think. Re-read the whole project and assert:

```bash
# re-run the paginated search with fields=assignee,reporter, then:
awk -F'\t' '$2!="<EXPECTED_ASSIGNEE>" || $3!="<EXPECTED_REPORTER>"' verify.txt
# nothing printed ⇒ every issue conforms. This is the only real proof.
```

⚠️ **Dry-run first on one issue.** Prove the exact PUT body on a single key
and re-read it, *then* loop. And these writes are **not undoable in bulk** —
Jira keeps a per-issue changelog, but there is no "revert the last 79 edits"
button. Capture the before-state (the same paginated read) if you might need
to reconstruct it.

## 8. Gotchas

- **`401 Unauthorized; scope does not match`** — the token authenticated but
  lacks a scope the endpoint requires. Not a credential problem; see §5.
- **`401` from `/oauth/token/accessible-resources`** — that endpoint is for
  **OAuth 3LO access tokens** and rejects API tokens, whichever kind. It is
  *not* evidence that the gateway is broken or that your token is bad — it's
  the wrong endpoint. Resolve the cloud id from `_edge/tenant_info` (§1),
  which needs no auth at all.
- **People fields need `accountId`, not email** — `{"assignee":{"accountId":…}}`.
  An email in that field is rejected. Resolve via `$BASE/user/search` (§6).
- **`creator` cannot be changed** — by any API, ever (§6). If it's wrong, the
  only fix is to have created the issue as the right account. `reporter` is
  the mutable one.
- **`401 AUTHENTICATED_FAILED`** (`x-seraph-loginreason` header) — a scoped
  token used against the **site domain**. Switch to the gateway (§0/§1).
- **`404 "Issue does not exist or you do not have permission to see it."`** —
  Jira masks missing *permission* as `404` (not `403`). If the key is
  correct, suspect scope/permission, not a typo.
- **`POST …/transitions` returns `204` with an empty body** — success has no
  JSON; don't try to parse it. Re-`GET` the issue to confirm the new status.
- **Transitions are current-status-dependent** — the id for a target status
  can differ (or be absent) depending on where the issue is now. Always
  resolve the id from a fresh `GET …/transitions`; never hard-code it.

---

## 9. The `jira.sh` client — command surface & `--role` auth

The skills don't call `curl` — they call `jira.sh` (POSIX) / `jira.ps1`
(Windows), a small client over the §0–§8 REST calls. It authenticates
**per-request** (Basic, §0): every invocation resolves its own credential and
sends it on that one call. There is no login step, no stored credential, and no
machine-global "active account" — which is what removes the single-account
race that a login-based CLI forced on parallel runs (the whole reason the
executor, assigner, and reviewer can run at once as different identities).

**`--role assigner|executor|reviewer`** (global; may appear anywhere in the
args) is **required** and picks which credential the call uses: it reads
`JIRA_<ROLE>_EMAIL` / `JIRA_<ROLE>_TOKEN` from `jira-sdlc-tools.local.env`.
Both are required for the role you name — there is no default account, so an
unset pair is an error rather than a silent borrow of another identity, and a
call with no `--role` is a usage error (exit 2). Every caller therefore names
its role: each skill passes its own, `check_assignee` passes the role whose
ownership it's checking, and `statuscheck --role <caller>` probes that one
role's credential in its `jira_auth` row.

### Command surface

```
jira --role assigner|executor|reviewer <command>

  whoami                                    who this credential authenticates as (GET /myself)
  project exists  <KEY>                     is the project visible to this account?
  issue view      <KEY> [--fields a,b,c]    get an issue — raw JSON on stdout (§10)
  issue create    --project K --type T --summary S
                  [--parent K] [--assignee email|@me]
                  [--desc-file FILE | --adf-file FILE]   -> prints the new key on stdout
  issue transition <KEY> --to "In Review"   transition by target status NAME (resolves the id, §4)
  issue assign     <KEY> (--to email|@me | --remove)
  issue comment add  <KEY> (--body-file FILE | --adf-file FILE)   (§11)
  issue comment list <KEY>                  raw JSON on stdout
  issue attach    <KEY> <FILE>                  upload a file attachment
  issue delete     <KEY> [--with-subtasks]
  raw <METHOD> </PATH> [--data-file FILE]   escape hatch; PATH is under /rest/api/3 (e.g. /myself)
```

Note the shape shifts from the old CLI: the key is **positional everywhere**
(no `--key`); reads are **always JSON** (no `--json` flag); `transition` takes
the **target status name** via `--to` (not `--status … --yes`); there is **no
inline comment body** — comments always come from a file (§11); and `create`
**prints the new key** on stdout, nothing to parse out of a browse URL.

### Output & exit contract

- **Reads** (`whoami`, `issue view`, `comment list`) print **raw JSON** on
  stdout — pipe it to `jq`. **Writes** (`transition`, `assign`, `comment add`,
  `delete`) print **nothing** on success (REST returns `204`). **`create`**
  prints just the new key. Errors go to **stderr**.
- Exit codes: `0` ok · `1` transport · `2` usage · `3` auth (401) · `4`
  not-found/permission (404) · `5` validation (400) · `6` forbidden (403) ·
  `7` unexpected · `8` no such transition. A non-zero exit from a skill's
  `jira` call is a stop condition — relay the stderr line, don't retry blindly.

### Dispatch & config

Ships as a contract pair: `bash …/scripts/posix/jira.sh` on Linux/macOS,
`pwsh`/`powershell …/scripts/win/jira.ps1` on Windows (identical args, output,
exit codes). It resolves `.jst/jira-sdlc-tools.env` / `.jst/….local.env` from
the **git top-level**, so **run it from inside the repo/worktree**; from an unrelated
directory it can't find config and stops with `JIRA_ACCOUNT_URL is unset`.

## 10. Issue field lists

`jira.sh issue view <KEY>` returns the issue as JSON (it's `GET /issue/<KEY>`,
which returns *all* fields by default). Pass **`--fields a,b,c`** to narrow the
payload to just what you parse — smaller, and explicit about what the caller
depends on. This toolkit uses two canonical field lists — the **single source
of truth**; the skills cite them by name rather than re-listing them:

| canonical list | `--fields` value | used by |
|---|---|---|
| **fetch-with-comments** | `summary,description,issuetype,status,parent,subtasks,comment` | `jira-task-executor` step 1 — it scans `fields.comment.comments` for the assigner's assignment report + `Task memory` notes (step 4) |
| **review-fetch** | `summary,description,issuetype,status,parent,subtasks` | `jira-task-reviewer` — it doesn't read comments, and `comment` dominates the payload on comment-heavy issues, so omitting it shrinks the parent + every per-sub-task fetch |

`subtasks` comes back as an array of `{"key": …, "fields": {"summary": …}}`,
so both `fields.subtasks[].key` and the nested `.fields.summary` are available.
`parent` and `comment` appear only when the issue actually has them (a leaf has
no `parent`; a non-parent's `subtasks` is `[]`), so naming them is safe on any
issue.

```bash
# executor fetch (with comments):
jira.sh issue view <KEY> --fields 'summary,description,issuetype,status,parent,subtasks,comment'
# reviewer fetch (no comments):
jira.sh issue view <KEY> --fields 'summary,description,issuetype,status,parent,subtasks'
```

### ⚠️ Comparing an assignee? Use `accountId`, never `emailAddress`

Jira returns `assignee.emailAddress` **only for your own account**. When the
issue is assigned to *anyone else*, the field is **absent from the object
entirely** — you get `accountId` and `displayName` and nothing more (a
user-privacy setting, on by default, not a permissions error):

```jsonc
// assigned to ME — email present
"assignee": { "accountId": "<MY_ACCOUNT_ID>", "displayName": "Task Executor",
              "emailAddress": "executor@example.com" }
// assigned to SOMEONE ELSE — no emailAddress key at all
"assignee": { "accountId": "<OTHER_ACCOUNT_ID>", "displayName": "Task Reviewer" }
// unassigned
"assignee": null
```

So an **email comparison can confirm a match but never detect a mismatch**:
"assigned to someone else" and "unassigned" both collapse to an empty string,
and code testing `email == mine` reports the wrong reason for the failure. This
was a live bug — an issue assigned to the reviewer reported to the executor as
*unassigned*. **Compare `accountId`**, which is always present on both sides:
`jira.sh --role <role> whoami` returns the caller's own (`.accountId`), and
every assignee object carries it. Use `displayName` for the human-readable
message, never for the comparison.
`skills/_shared/scripts/posix/check_assignee.sh` is the invoked implementation.

## 11. Comments & markers

### Add a comment

```bash
jira.sh issue comment add <KEY> --body-file /tmp/comment.txt   # plain text
jira.sh issue comment add <KEY> --adf-file  /tmp/comment.adf   # a bare ADF "doc" object
```

There is **no inline `--body`** — a comment body always comes from a file. With
`--body-file`, `jira.sh` turns the plain text into ADF, **one paragraph per
non-blank line**, so markdown syntax (`##`, `-`, `1.`) is stored *literally* as
text, not rendered. For real structure — headings, lists, code blocks — build
an ADF `doc` object yourself and pass it with `--adf-file`. Because the body is
read from a file, backticks and other shell-active characters in it are never
at risk of command substitution.

```bash
cat > /tmp/c.txt <<'EOF'
Multi-line comment body in plain text.
Backticks (`like this`) are literal — they're in the file, not the shell.
EOF
jira.sh issue comment add <KEY> --body-file /tmp/c.txt
```

### Machine-recoverable comment markers

Some comments carry a fixed leading marker so a later session (or a human) can
grep them back out. `jira.sh` stores a plain-text line as a single ADF text
node verbatim, so the marker survives round-trip and a `grep` over
`issue comment list <KEY>` JSON still finds it. Mirror the exact prefix when
posting, match on it when reading:

- `PR target branch: <branch>.` — the PR base for the issue's branch, posted by
  `jira-task-assigner` (or the no-assigner provisioning, §12) and consumed by the
  §13 PR-base resolver.
- `Task memory (jira-task-executor)` — a durable per-task memory note the
  executor leaves for future sessions (findings, gotchas, design decisions +
  rationale, recovery context). Deliberately distinct from the executor's
  single end-of-run report and from the `PR target branch:` line, so grepping
  the marker returns only memory notes.
- `Assignment report` — the assigner's end-of-run report (step 7): issue keys
  and links, the scope decision, the base path taken, and the branch/worktree
  layout. Posted on the **top-level** issue, so a sub-task's own comments won't
  carry it — the executor reads it for the planning context behind the issue
  it's picking up.

### List a work item's comments

```bash
jira.sh issue comment list <KEY>        # raw JSON on stdout
```

## 12. Git branch convention

Every change goes on its own branch, `feature/<KEY>-<slug>` or
`hotfix/<KEY>-<slug>` — no "small enough to commit straight to the working
branch" shortcut. **The prefix follows the base branch, not the issue type**
(SDLC.md §2): `feature/` = branched from `<DEFAULT_BASE_BRANCH>`
(`development`), covering all planned work — features *and* bug fixes alike;
`hotfix/` = an emergency fix branched from `<PRODUCTION_BRANCH>`.
`jira-task-assigner` pre-creates the branch and worktree for every leaf issue
and takes its prefix from the base it was told to use (its step 5C):
`feature/` by default, `hotfix/` only when the user explicitly asks for the
emergency production flow, in which case it cuts a single leaf from
`origin/<PRODUCTION_BRANCH>`. So a `hotfix/` branch comes from either that
path or the no-assigner provisioning below.

GitHub-for-Jira links a branch to an issue purely by finding the issue key
inside the branch name — no API call required.

```
git checkout -b feature/<ISSUE-KEY>-<slugified-summary>
git push -u origin feature/<ISSUE-KEY>-<slugified-summary>
```

Slugify the title: lowercase, spaces → hyphens, strip punctuation.
`"Fix null pointer on login!"` → `fix-null-pointer-on-login`.

### No-assigner provisioning (issue with no branch/worktree yet)

> **Two unrelated senses of "bootstrap" live in this plugin — don't conflate
> them.** This section is *git-level* setup: creating a branch and worktree for
> an issue the assigner never touched, done **before** the executor runs.
> `.jst/bootstrap.sh` / `.jst/bootstrap.ps1` is a different thing entirely — an
> optional project-owned hook that provisions the *app's runtime* (database,
> ports, deps) **inside** an already-existing worktree, run by the executor's
> step 1 (`project-config.md` § *the optional worktree hook*). This section
> creates the worktree; that hook makes one runnable.

`jira-task-executor` never creates the issue branch — it derives the issue key
*from* the branch it's standing on, so there is no state where it runs and the
branch is missing. When an issue was created without `jira-task-assigner` (e.g.
an ad-hoc `Bug`), provision it manually **before** invoking the executor:

1. Pick the prefix from the **base branch you're branching from**:
   `<PRODUCTION_BRANCH>` (an emergency production fix) → `hotfix/`; any other
   base, such as `<DEFAULT_BASE_BRANCH>` (`development`) → `feature/`.
2. From the intended base branch — checked out and up to date with origin;
   this is what the PR will target:
   ```bash
   BASE=$(git branch --show-current)
   git worktree add <WORKTREES_DIR>/worktree-<KEY> -b <prefix>/<KEY>-<slug> "$BASE"
   git config branch."<prefix>/<KEY>-<slug>".parentbranch "$BASE"
   ```
3. Post the durable PR-base fallback the assigner normally posts. `jira.sh`
   has no inline body, so write the one line to a file first (§11):
   ```bash
   printf 'PR target branch: %s.\n' "$BASE" > /tmp/prbase.txt
   jira.sh issue comment add <KEY> --body-file /tmp/prbase.txt
   ```
4. `cd` into the new worktree and run the executor.

## 13. PR-base resolver

Every leaf issue's PR needs a base branch. The assigner records it in two
places — one local (`git config branch.<branch>.parentbranch`), one durable (a
`PR target branch: …` Jira comment that survives a fresh clone). The resolver
checks both, then — for a sub-task, whose real base is its parent's branch and
never the env default — recovers by searching for that parent branch.

**It is a script, not a snippet to copy.** `_shared/scripts/posix/pr_base.sh`
and its `win/pr_base.ps1` twin are the implementation; this section documents
them. Hand-copying the logic into a skill is what let the copies drift apart
and left Windows with no runnable resolution at all.

```bash
bash pr_base.sh --role <role> [--branch <BRANCH>] [--parent-key <PARENT-KEY>] [ISSUE-KEY]
```

`--parent-key` is not an env var: it's the leaf's `fields.parent.key` from the
issue fetch (§10), and omitting it for a sub-task is what would wrongly let the
env default through. `--branch` resolves the base for a branch other than the
checked-out one — `jira-task-reviewer` needs it, because it resolves
`<PARENT-BRANCH>`'s base while standing in a sub-task's worktree, where the
current branch is the sub-task's; branch config lives in the shared
`.git/config`, so this works from any worktree. `ISSUE-KEY` defaults to the key
derived from `--branch`, or from the current branch when that is absent. It
prints exactly two lines and exits non-zero when unresolved:

```
base=<branch>     # empty when unresolved
source=git-config|jira-comment|branch-search|env-default|unresolved
```

Exit 1 with `source=unresolved` means a sub-task whose parent-branch search
matched zero or several branches: **STOP, ask the user, do not open the PR.**
Exit 2 is a usage or environment error. What to do with each *resolved* source
is the calling skill's judgment, not the script's — see below.

The `PR target branch:` marker sits verbatim in an ADF text node (§11), so the
script's `grep` over `jira.sh issue comment list` JSON matches it exactly as it
did the old CLI's output. Sources, in the order tried:
1. `source=git-config` — `git config branch.<current>.parentbranch`, set by the
   assigner when the branch was created; local to this clone.
2. `source=jira-comment` — the issue's `PR target branch: …` comment, the
   durable fallback the assigner posts (or the no-assigner provisioning does,
   §12); survives a fresh clone or a different machine.
3. `source=branch-search` — sub-tasks only, i.e. when `--parent-key` is
   non-empty. Searches for a `feature/<PARENT-KEY>-*` / `hotfix/<PARENT-KEY>-*`
   branch, deduping the local and `remotes/origin/` copies (§12) so a pushed
   branch counts once.
   - Exactly one match → use it, and say in the report the base was **recovered
     by branch search**, not read from the primary sources.
   - Zero or multiple matches → `base=` empty, `source=unresolved`, exit 1.
     **Stop before `gh pr create` and ask the user** — do not fall back to
     `<DEFAULT_BASE_BRANCH>`, which is never a sub-task's base.
4. `source=env-default` — `<DEFAULT_BASE_BRANCH>` from
   `.jst/jira-sdlc-tools.env`, reachable **only for a top-level issue** (no
   `--parent-key`) and correct for a planned-work one. Still call it out in the
   report, and check it against the prefix first (below).

### Sanity check: the prefix and the base have to agree

§12 ties the prefix to the base branch, so for a **top-level** issue (empty
`PARENT_KEY` — the only case that can reach source 4) the two are a matched
pair, and a disagreement means one of them is wrong:

- branch `hotfix/…` with a base that isn't `<PRODUCTION_BRANCH>`
- branch `feature/…` with a base that *is* `<PRODUCTION_BRANCH>`

**Stop and ask which is right** rather than opening the PR. The first case is
the one that actually happens: a hotfix whose `PR target branch:` comment was
never posted has nothing to stop it falling through to source 4, and a
production fix quietly retargeted at staging neither reaches production nor
gets versioned (`hotfix/*` is what CI patch-bumps — SDLC.md §5). Sub-tasks are
exempt: a sub-task's base is its parent's branch, which carries the parent's
prefix rather than a long-lived branch name.
