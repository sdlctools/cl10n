---
description: >-
  Operational reference for the versioning workflows — dev builds
  (tag-development-rc.yml), the release cut (cut-release.yml) and the release
  itself (release.yml): the two tag shapes, who owns the version number at
  each step, the invariants that keep the pipeline firing, and a failure-mode
  table. Read before changing anything under .github/workflows/, cutting a
  release, or diagnosing a release that did not happen.
paths:
  - .github/workflows/**
---

# Release pipeline

AGENTS.md → *Releasing* is the happy path: dispatch the cut, QA on the
branch, merge into `main`, and `release.yml` tags, publishes, bumps
`pyproject.toml`, back-merges and cleans up. **This file is the mechanics
underneath it** — what fires what, what each workflow may and may not
assume, and the ways the chain breaks silently. It does not restate the
phases AGENTS.md already describes.

## The version space

Two tag shapes, and the difference matters to every version query here:

| Shape | Created by | On | Meaning |
| --- | --- | --- | --- |
| `vX.Y.Z` | `release.yml` | the merge commit on `main` | a real release; a GitHub Release exists |
| `vX.Y.Z-dev.N` | `tag-development-rc.yml` | a commit on `development` | a dev build; `X.Y.Z` is the *last* release, `N` counts pushes since |

`X.Y.Z` in a dev tag never moves on its own. It is pinned to the newest
plain release tag and changes only when a real release ships, at which point
`N` restarts from 1.

Both version-resolving workflows therefore filter to **plain** tags —
`cut-release.yml` (`grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$'`) and `release.yml`
(`git describe --exclude '*-*'`). This is not cosmetic: `sort -V` ranks
`v0.1.0-dev.3` *above* `v0.1.0`, so an unfiltered "latest tag" would feed a
prerelease into SemVer arithmetic that rejects it and the cut would fail.
Any new tag query must keep the filter.

## What fires what

```
push to development ──► tag-development-rc.yml   stamp pyproject, commit, tag vX.Y.Z-dev.N
                    ──► checks.yml               the CI gate
                    ──► cl10n.yml                (only when md/** changed)

human dispatches "Cut release" (patch|minor|major)
                    ──► cut-release.yml          latest plain tag + bump -> X.Y.Z
                                                 branch release/sprint-X.Y.Z off development
                                                 empty commit "chore: cut …"   ◄── load-bearing
                                                 draft PR into main

QA lands as PRs into release/sprint-X.Y.Z (never features, never into main)

human marks the draft ready and merges into main
                    ──► release.yml              version from the BRANCH NAME
                                                 tag vX.Y.Z on the merge commit
                                                 gh release create (notes since previous plain tag)
                                                 bump pyproject on main, push
                                                 build wheel+sdist from the BUMPED tree
                                                 back-merge main -> development (PR on conflict)
                                                 delete release/sprint-X.Y.Z
                                            └──► publish-pypi   upload to PyPI (OIDC)
```

`publish-pypi` is a second job in the same file, `needs: release`. It is
separate because both things it requires are job-scoped — `id-token: write`
for the OIDC claim PyPI verifies, and `environment: pypi`, which is what a
trusted publisher is registered against. Scoping them there also means the
release job never holds a credential that can publish a package. It runs
last, after the tag, the Release, the bump and the back-merge, so a failed
upload never strands the repository mid-release.

A `hotfix/*` PR merged into `main` enters `release.yml` at the same point,
differing only in version resolution: no branch-name parse, a forced patch
bump off the latest plain tag. A hotfix before the first release is an
error, by design.

## Who owns the version number

| Step | Source of truth |
| --- | --- |
| dev build | latest plain tag + `-dev.<N+1>` — derived, never authored |
| release cut | latest plain tag + the dispatch `bump` input |
| release | **the branch name**, `release/sprint-<X.Y.Z>`, no leading `v` |
| hotfix | latest plain tag, patch-bumped |

`release.yml` accepts exactly one spelling and fails loudly on anything else
— no fallback to tag arithmetic, no PR label. To ship a different version,
rename or re-cut the branch. That single rule is why the cut and the release
cannot disagree about what is being shipped.

## Invariants — do not violate these

### 1. The release branch's tip commit must not carry a CI skip marker

`cut-release.yml` adds an empty `chore: cut release/sprint-X.Y.Z` commit
before pushing the branch. **It is load-bearing, not cosmetic.** Delete it
and the release pipeline silently stops working.

`tag-development-rc.yml` commits the dev stamp with `[skip ci]` in the
message, so `development`'s tip almost always carries that marker. A branch
cut off `development` with no commits of its own inherits that commit *as
its own tip*. GitHub reads the skip marker off the **HEAD commit of the
pull_request event**, so merging such a PR into `main` emits **no workflow
runs at all** — not `release.yml`, not `checks.yml`, not the Jira
transition. No tag, no GitHub Release, no version bump, no back-merge, no
branch cleanup, and no failed run to notice.

This is not hypothetical here: `v0.1.0` was released this way. PR #9
(`release/sprint-0.1.0` → `main`) had tip `chore(dev): 0.0.0-dev.1
[skip ci]`, produced zero runs on merge, and the tag, the GitHub Release
and the `pyproject.toml` bump were all done by hand afterwards. The
repository has never had a successful `Release` run.

Verified by A/B on throwaway branches in the sibling repo that shares these
workflows: bot-authored (`GITHUB_TOKEN`) PRs, drafts marked ready, and human
PRs all fire normally; a `[skip ci]` tip fires nothing; the same branch plus
one empty commit fires again.

Corollaries:

- **Never put the literal skip marker in a commit subject that can become a
  PR head tip.** A commit that merely *describes* the marker suppresses its
  own PR's runs — including its `checks.yml` gate.
- If `[skip ci]` is ever removed from `tag-development-rc.yml`, the cut
  commit may go too — but not before, and the two comments cross-reference
  each other for that reason.

### 2. Pushes made by `GITHUB_TOKEN` do not trigger workflows

A human merging a token-created PR *does* trigger them; a `git push` from
inside a workflow does not. Consequences:

- `release.yml`'s back-merge into `development` creates **no** dev build and
  runs **no** checks. The first `vX.Y.Z-dev.1` of the new cycle appears on
  the next ordinary push.
- `release.yml`'s version-bump push to `main` triggers nothing either.
- `cl10n.yml`'s translation branch is pushed by the token, so its PR gets no
  runs until a human merges it — the merge itself does fire.
- If a workflow ever needs to trigger another workflow, it needs a PAT or a
  GitHub App token, not `secrets.GITHUB_TOKEN`.

### 3. The dev-build loop is broken by the marker, and only by it

`tag-development-rc.yml` pushes to the branch that triggers it. Three things
keep that from looping: `[skip ci]` on the stamp commit, the already-tagged
guard (`git describe --exact-match --match 'v*-dev.*' HEAD`), and the
`tag-development-dev` concurrency group that serializes runs. The marker is
the primary stop. Removing it makes the guard the only stop, and a failure
between the commit push and the tag push then becomes an infinite commit
loop — which is why the fix for invariant 1 lives in `cut-release.yml`
instead.

### 4. Every workflow declares its own `permissions:` block

Nothing here may rely on the repository default for `GITHUB_TOKEN`. A
workflow that omits the block cannot tag, push, or open a PR, and fails at
the first write with a permission error.

### 5. `gh pr create --draft` needs the repo setting

"Allow GitHub Actions to create and approve pull requests" must stay ON, or
`cut-release.yml`'s draft PR step fails — as does `release.yml`'s
conflict-path sync PR and `cl10n.yml`'s translation PR.

## Failure modes and what they mean

| Symptom | Cause | Fix |
| --- | --- | --- |
| Release PR merged, **zero** runs on the merge | head tip carries a skip marker (invariant 1) | re-cut the branch so its tip is the `chore: cut` commit; recover as below |
| `Release` runs but fails at *Resolve version* | branch is not `release/sprint-<X.Y.Z>` — a leading `v`, a suffix, a rename | rename or re-cut the branch; do not patch the regex |
| `Cut release` fails: *Branch … already exists* | a previous release never completed, so `release.yml` never deleted it | delete the stale branch, then re-run the cut |
| `gh pr create`: *No commits between …* | `main` already contains everything on the cut branch | the `chore: cut` commit prevents this; if seen, the commit was removed |
| `Release` fails: *Tag … already exists* | the version was already released, or a tag was pushed by hand | do not overwrite; re-cut at the next version |
| Back-merge opened a PR instead of pushing | `main` and `development` diverged | resolve the sync PR by hand — never force-push `development` |
| `publish-pypi` fails with `invalid-publisher` | no trusted publisher registered on PyPI for this repo/workflow/environment | register it (AGENTS.md → Releasing lists the exact five fields); everything else in the release already succeeded, so re-run just this job |
| `publish-pypi` fails: *File already exists* | that version was already uploaded; PyPI files are immutable | do not try to overwrite — cut the next version |
| No `vX.Y.Z-dev.1` after a release | invariant 2, not a bug | it appears on the next push to `development` |

### Recovering a release that never fired

The pipeline is idempotent enough to re-run once the branch is sane:

1. `git push origin --delete release/sprint-<X.Y.Z>` — the stale branch
   blocks the next cut.
2. Re-run **Cut release** with the same bump. The version is derived from
   the latest plain tag, so a missed release does not skip a number.
3. Merge the new draft PR. `release.yml` tags, publishes, bumps,
   back-merges and cleans up.

`main` having already received the content is fine: the cut commit
guarantees a non-empty diff, and the tag lands on the new merge commit. If a
tag was created by hand for the missed release, that version is spent — cut
the next one rather than trying to re-tag it.
