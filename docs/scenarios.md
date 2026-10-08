# Failure scenarios

The generator can fail a build in six ways. They are used two ways:

- **Background mode** sprinkles random failures into the normal flow (`--failure-rate`, default `0.12`).
- **Scenario mode** plays a scripted story of consecutive builds on demand, so the demo does not depend on luck:

```bash
python3 pipeline_log_generator.py --list-scenarios
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs --scenario bad-commit
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs --scenario bad-commit cve-blocked
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs --scenario all --scenario-gap 30
```

Scenario mode exits when the story is finished. It can run **alongside** the background process, because build
numbers are claimed atomically.

## Failure kinds

| Kind | Breaks stage | What the log shows |
|---|---|---|
| `compile-error` | Build | Maven `COMPILATION ERROR`, `cannot find symbol`, `BUILD FAILURE` |
| `unit-test-failure` | Unit Test | Surefire failure with an assertion or `NullPointerException` stack trace |
| `cve-critical` | Security Scan | Trivy table with a critical (fictional) CVE, then `Security gate FAILED` |
| `deploy-qa-failure` | Deploy QA | `helm UPGRADE FAILED`, pods in `ImagePullBackOff` or `CrashLoopBackOff`, `kubectl describe` events |
| `e2e-failure` | E2E Test | WebdriverIO `✖` results and `Error: element (...) still not displayed` stack traces |
| `prod-rollback` | Deploy Prod | Canary error rate climbs, `ERROR: canary analysis failed`, `helm rollback` |

Everything after the failing stage is logged as `Stage "X" skipped due to earlier failure(s)`, followed by a
`Declarative: Post Actions` block with `Pipeline failed in stage: <stage>` and `Finished: FAILURE`.

**Same commit, same failure.** Failure details (failing test names, selector, symbol, library, error rates) are
seeded from the commit SHA, so rebuilding the same commit reproduces the same failure, as it would in real life.

## Scenarios

| Name | Job | Story (build outcomes in order) |
|---|---|---|
| `bad-commit` | portal-ui | `e2e-failure` -> `e2e-failure` (rebuild) -> `e2e-failure` (rebuild) -> SUCCESS with a fix commit |
| `cve-blocked` | claims-service | `cve-critical` -> SUCCESS after the dependency bump |
| `prod-rollback` | payment-api | `prod-rollback` -> SUCCESS with a fix commit |
| `broken-build` | claims-service | `compile-error` -> `compile-error` (quick fix also broken) -> SUCCESS |
| `test-regression` | payment-api | `unit-test-failure` -> SUCCESS |
| `flaky-qa-deploy` | portal-ui | `deploy-qa-failure` -> SUCCESS on a retry of the *same* commit |

Rebuilds show `Started by user <name>` instead of `Started by GitHub push by <name>`.

### Which scenario to use for which message

| Message you want to land | Scenario |
|---|---|
| "Find the exact commit and author that broke the pipeline, and how long recovery took" | `bad-commit` |
| "Security issues are caught in the pipeline, before QA or production" | `cve-blocked` |
| "A release can pass every test gate and still hurt production; link the deployment to the impact" | `prod-rollback` |
| "Separate real defects from infrastructure noise" | `flaky-qa-deploy` |

## Finding the scenario builds afterwards

Every scenario build is recorded in `<base-dir>/_scenarios/manifest.jsonl` (outside `jobs/`, so the log agent
never ingests it):

```json
{"job": "portal-ui-ci-cd", "build": 1042, "status": "FAILURE", "commit": "7c3d9e1", "branch": "main",
 "author": "rahul-m", "failed_stage": "E2E Test", "failure": "e2e-failure", "scenario": "bad-commit", "step": 1, "time": "..."}
```

Use `job` + `build` to filter in Dynatrace (`pipeline.name` and `build.number`).

## Sample excerpts

**`cve-blocked`** (Security Scan stage of the failing build, Jenkins timestamps removed for readability):

```
+ trivy image --severity HIGH,CRITICAL --exit-code 1 registry.corp.example.com/claims-service:a202904
2026-10-08T14:46:37.588Z	INFO	Vulnerability scanning is enabled
...
Java (jar)
==========
Total: 1 (HIGH: 0, CRITICAL: 1)

┌─────────────────────────────────────┬────────────────┬──────────┬────────┬───────────────────┬───────────────┐
│ Library                             │ Vulnerability  │ Severity │ Status │ Installed Version │ Fixed Version │
├─────────────────────────────────────┼────────────────┼──────────┼────────┼───────────────────┼───────────────┤
│ com.corp.thirdparty:document-parser │ CVE-2026-99001 │ CRITICAL │ fixed  │ 2.3.1             │ 2.3.4         │
└─────────────────────────────────────┴────────────────┴──────────┴────────┴───────────────────┴───────────────┘

Security gate FAILED: 1 critical vulnerability exceeds policy threshold (max allowed: 0)
ERROR: script returned exit code 1
```

**`prod-rollback`** (Deploy Prod stage and ending):

```
+ ./scripts/canary-analysis.sh payment-api --duration 5m --max-error-rate 5.0
Canary analysis: baseline error rate 0.3%, canary error rate 1.5%
WARNING: canary error rate 1.5% is above baseline 0.3%
Canary analysis: baseline error rate 0.3%, canary error rate 6.5%
Error: 182 requests to /v1/payments failed in the last 2 minutes (HTTP 504 Gateway Timeout)
ERROR: canary analysis failed: error rate 6.5% exceeds threshold 5.0%
+ helm rollback payment-api 140 --namespace prod --wait
Rollback was a success! Happy Helming!
...
Pipeline failed in stage: Deploy Prod
Finished: FAILURE
```

## Important limits

- **The CVE ids and libraries are fictional.** Do not present them as real vulnerabilities.
- **The prod-rollback story only exists in the pipeline log.** The application-side error spike (a metric or
  traces from the running service) is not generated; to show the "deployment caused this" link end to end you
  need real or separately simulated application telemetry.
- Logs are synthetic. Frame them as "what the data looks like once the pipeline is integrated", not as logs
  from the customer's own tools.

## Adding your own scenario

1. Add an entry to `SCENARIOS` in `pipeline_log_generator.py` (job, branch, author, commit messages, steps).
2. To add a new failure kind, add it to `FAILURE_STAGE` / `FAILURE_WEIGHTS`, handle it in the matching
   `stage_*` function (emit the tool output, then call `fail_exit`), and add the stage to the expectations in
   `tests/test_generator.py`.
3. Run `python3 -m unittest discover -s tests -v`.
