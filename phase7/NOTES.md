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

`on: pull_request` makes the check run and report a result. It does NOT
by itself block a merge -- that requires enabling "Require status checks
to pass before merging" for the `eval` check under Settings > Branches
in the GitHub UI, a one-time manual step outside version control.
