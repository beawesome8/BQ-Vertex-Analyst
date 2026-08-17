# Phase 7 Notes

GitHub Actions workflow running the eval harness on every PR and push to
`main`, blocking merge on regression via the harness's own exit code.

## The original plan didn't survive contact with GCP's current defaults

Initial approach: create a CI service account, generate a JSON key,
store it as a GitHub secret, authenticate with `credentials_json`. Key
creation failed immediately:

```
FAILED_PRECONDITION: Key creation is not allowed on this service account.
```

This is Google's `constraints/iam.disableServiceAccountKeyCreation` org
policy, enabled by default on current projects specifically because
long-lived keys are a real, common leak vector. Rather than override the
policy to force the original plan through, switched to Workload Identity
Federation: GitHub Actions exchanges a short-lived OIDC token at runtime,
no key ever exists, so the policy that blocked the original approach
doesn't even apply. This is also the approach Google's own
`google-github-actions/auth` documentation recommends over keys --
the org policy forced the more secure design, not a worse one.

Confirmed live in the run log: `Authenticate to GCP` created a
credentials file at a runner-local temp path
(`/home/runner/work/.../gha-creds-....json`) that exists only for the
duration of the job -- not a secret pulled from storage, a token
generated fresh each run.

## Near-miss: an empty key file, caught before it mattered

The failed key-creation command left a 0-byte `ci-key.json` sitting
untracked in the repo working directory. Checked its contents (size and
JSON structure, without printing key material into any log or
conversation) before deleting it -- confirmed genuinely empty, not a
real credential. No exposure occurred, but `.gitignore` had no pattern
that would have caught a real key file if the command had partially
succeeded instead of failing cleanly. Added `*key*.json` and
`ci-key.json` patterns as defense in depth, even though WIF means no key
file should exist in this project at all going forward.

## Safety-critical trigger choice

Workflow uses `pull_request`, not `pull_request_target`. The latter
exposes repository secrets to PRs from forks -- a known supply-chain
attack vector. `pull_request` does not, which is the correct default for
a public repo. Documented directly in the workflow file's comments so
this is never "fixed" into the dangerous version by someone (including a
future me) trying to make a fork PR's missing-auth failure go away.

## Verified live, not just written

Two independent successful runs, different triggers:
- `push` to `main` (from committing the workflow file itself)
- `pull_request` (from a real test PR, closed without merging once
  verified)

Confirmed inside the actual log, not inferred from a green checkmark:
`TOTAL: 8/8 passed (100%)`, adversarial guard catch rate 1.0, scalar
accuracy rate 1.0 -- the real eval harness result, run against live
Gemini and BigQuery from GitHub's infrastructure.

One incidental finding: average latency from GitHub's runners was 7.3s,
roughly half the 14.9s measured running locally -- consistent with
GitHub's runners being geographically closer to the `us-central1` region
than a home connection, not a fluke or a regression.

## Known, non-blocking signal for later

Workflow annotations show a Node.js 20 deprecation warning -- the pinned
action versions (`checkout@v4`, `setup-python@v5`, `upload-artifact@v4`,
`setup-gcloud@v2`) are being auto-forced onto Node 24 by GitHub rather
than failing. Nothing broken today; worth a version bump before GitHub
stops the auto-forcing and it becomes an actual failure instead of a
warning.

## Branch protection is a manual step, not something a workflow file can set

`on: pull_request` makes the check run and report a result. Enabling
"Require status checks to pass before merging" for `eval` under
Settings > Branches makes GitHub display and require that check.

This went through two real, observed states while finishing this phase,
not one assumed state -- worth recording both, since the second
correction proves why guessing from a single observation is a mistake:

1. **First observed state:** a direct push to `main` succeeded with
   `remote: Bypassed rule violations for refs/heads/main: - Required
   status check "eval" is expected`. At that point, "Do not allow
   bypassing the above settings" was unchecked -- GitHub's default lets
   repo admins bypass their own branch protection.
2. **Second observed state, after enabling "Do not allow bypassing":** a
   subsequent direct push was flatly rejected --
   `GH006: Protected branch update failed for refs/heads/main`,
   `[remote rejected] main -> main (protected branch hook declined)`.

Current, confirmed state: **both "Require status checks to pass" (with
`eval` selected) and "Do not allow bypassing the above settings" are
enabled.** This is genuine full enforcement -- the `eval` check must
pass before ANY change reaches `main`, including the repo owner's own
direct pushes, with no bypass available to anyone. Getting this correction
itself onto `main` required going through a real pull request rather
than a direct push, which is itself live proof the rule now holds.

## The enforcement mechanism found its own bug

Getting the branch-protection correction onto `main` via a real PR
surfaced a genuine design flaw: the workflow's `paths:` filter listed
`phase1/**` through `phase6/**` explicitly. `phase7/NOTES.md` matched
nothing in that list, so the `eval` check never triggered for this PR
at all -- and a "Required" check that never runs doesn't satisfy branch
protection, it just waits forever. The PR was structurally unable to
merge, not because of anything wrong with its content, but because the
trigger conditions and the merge requirement contradicted each other.
This would have hit the identical wall on Phase 8 even without this
correction PR surfacing it now.

Fixed by replacing the hardcoded per-phase list with a single glob
(`phase*/**`), closing this bug class for every future phase directory
permanently rather than patching it phase-by-phase. Tradeoff, stated
plainly: this means a future documentation-only change inside any phase
folder will now also trigger a full, real, costly eval run -- a real
cost increase versus the original design intent, deliberately traded for
"a required check can never deadlock itself again."

The fix commit itself only merged because it modified
`.github/workflows/eval.yml`, which the (buggy) path filter did still
match -- meaning the bug was fixed via a PR that could only succeed
because it happened to touch the one path the broken filter still
covered. Worth remembering as a genuinely good debugging story: the
system's own required-check mechanism caught a gap in itself, and the
fix was verified by the same mechanism it corrected, live, not asserted.

## Final state, confirmed by an actual passing merge

`beawesome8 merged 3 commits into main from fix-phase7-docs-2` --
`2 checks passed`, merge commit `65b348d`. This is the end-to-end proof
the whole system works: a required check that genuinely blocks until it
passes, authenticated via WIF with no stored key, correctly triggered by
the path-filter fix, run against live Gemini and BigQuery, and only then
allowing the merge through. Confirmed via `git log` on `main` after
`git pull`, not inferred from the GitHub UI alone.