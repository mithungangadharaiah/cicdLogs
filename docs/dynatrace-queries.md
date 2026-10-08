# Dynatrace queries (DQL)

> **Status: not verified against a live tenant.** Field names follow the Bindplane processor in this repo
> (`pipeline.name`, `build.number`, `pipeline.status`, ...). Run each query once, compare with what you
> expect, and adjust. Ingest-time fields may appear under different names depending on how your tenant
> maps OTLP attributes (check one record with `fetch logs | filter log_type == "cicd-sdlc" | limit 1`).

All queries go in a **Notebook** against the tenant that receives the **structured** lane.

## 1. Sanity check: what arrives

```
fetch logs
| filter log_type == "cicd-sdlc"
| sort timestamp desc
| limit 30
| fields timestamp, loglevel, pipeline.name, build.number, pipeline.stage,
         git.commit, git.branch, pipeline.status, content
```

Expect `pipeline.name` and `build.number` on every row, and the other columns filled only on the specific
lines that carry them (this is by design).

## 2. One row per build (the stitched view)

```
fetch logs
| filter log_type == "cicd-sdlc"
| summarize
    commit       = takeAny(git.commit),
    branch       = takeAny(git.branch),
    author       = takeAny(git.author),
    status       = takeAny(pipeline.status),
    failed_stage = takeAny(pipeline.failed_stage),
    started      = min(timestamp),
    ended        = max(timestamp),
    lines        = count(),
    errors       = countIf(loglevel == "ERROR"),
    warnings     = countIf(loglevel == "WARN"),
    by: {pipeline.name, build.number}
| fieldsAdd status   = coalesce(status, "RUNNING"),
            duration = ended - started
| sort started desc
```

If `commit`, `status` or `failed_stage` come back null although the lines exist, `takeAny` is returning a
null value for that group. Workaround: build the sub-tables separately and join them, e.g. filter to
`isNotNull(pipeline.status)` for the status table and `isNotNull(git.commit)` for the commit table, then
`join` on `{pipeline.name, build.number}`.

## 3. Failure analysis

Build outcomes per pipeline:

```
fetch logs
| filter log_type == "cicd-sdlc" and isNotNull(pipeline.status)
| summarize builds = count(), by: {pipeline.name, pipeline.status}
| sort pipeline.name asc
```

Which stage breaks most often:

```
fetch logs
| filter log_type == "cicd-sdlc" and isNotNull(pipeline.failed_stage)
| summarize failures = count(), by: {pipeline.failed_stage}
| sort failures desc
```

Failed builds with the commit that caused them (join the failure line with the checkout line):

```
fetch logs
| filter log_type == "cicd-sdlc" and (isNotNull(pipeline.failed_stage) or isNotNull(git.commit))
| summarize
    failed_stage = takeAny(pipeline.failed_stage),
    commit       = takeAny(git.commit),
    author       = takeAny(git.author),
    message      = takeAny(git.commit_message),
    by: {pipeline.name, build.number}
| filter isNotNull(failed_stage)
| sort build.number desc
```

Most common error lines:

```
fetch logs
| filter log_type == "cicd-sdlc" and loglevel == "ERROR"
| summarize occurrences = count(), by: {content}
| sort occurrences desc
| limit 15
```

## 4. Drill into one build

```
fetch logs
| filter log_type == "cicd-sdlc"
| filter pipeline.name == "portal-ui-ci-cd" and build.number == 1042
| sort timestamp asc
| fields timestamp, loglevel, pipeline.stage, content
```

(Replace the name and number; the scenario manifest tells you which builds belong to a scenario.)

## 5. Ideas not implemented yet

- **Per-stage duration:** needs the time between consecutive stage-marker lines of the same build; verify the
  DQL window/ordering functions available in your tenant before building it.
- **Retry detection / mean time to recovery:** group by `pipeline.name`, find a failure followed by a success on
  a later `build.number`, and subtract the timestamps.
- **DORA-style metrics** (deployment frequency, change failure rate, lead time) from the `Deploy Prod` stage
  and `pipeline.status`.
