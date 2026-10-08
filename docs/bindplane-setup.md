# Bindplane setup

This guide wires the generated log files into two destinations through one Bindplane collector:

| Lane | Source | Processing | Destination | Purpose |
|---|---|---|---|---|
| **Raw** | `CICD-Raw` (File) | none | Tenant B | Shows unstructured logs exactly as a CI tool writes them |
| **Structured** | `CICD-Structured` (File) | one custom `transform` processor | Tenant A | Shows the same logs parsed into fields |

Both lanes read the **same files**. Each File source keeps its own read position, so this is safe.

> Menu names vary slightly between Bindplane versions. If something does not match, trust the
> intent of the setting, not the exact label.

## 0. Prerequisites

- A collector with a configuration that already has working Dynatrace (OTLP) destinations for both tenants
  (API token with the logs ingest scope; the token lives in Bindplane, never in this repo).
- The generator running (or at least one build directory present).
- The collector's service user can read the log directory:
  ```bash
  ls -ld /data /data/CICDPipeLogs /data/CICDPipeLogs/pipeline-logs
  ```
  Every directory needs the execute bit for the collector user, and the log files need read access.

## 1. Add the raw source

Configuration -> **Open Advanced Editor** -> **Add Source** -> **File**

| Setting | Value |
|---|---|
| Short Description | `CICD-Raw` |
| File Path(s) | `/data/CICDPipeLogs/pipeline-logs/jobs/*/builds/*/log` |
| Log Type | `cicd-sdlc` |
| Multiline Parsing | `none` (every line already has its own timestamp, including stack-trace lines) |
| Include File Name Attribute | ticked |
| **Include File Path Attribute** | **ticked** (job name and build number exist only in the path) |
| **Start At** | **beginning** (new files are discovered just after creation) |
| Enable File Offset Storage | ticked |

Connect this source to **Tenant B's destination only**.

Why `Start At: beginning`: every build is a new file. With `end`, the collector would skip whatever was
written before it noticed the file.

## 2. Add the structured source

Same settings as above, with Short Description `CICD-Structured`. Connect it to **Tenant A's destination only**.

Click the processor box on this lane -> **Add Processor** -> **Custom** -> telemetry type **LOGS**, and paste
the contents of [`bindplane/cicd-structured-processor.yaml`](../bindplane/cicd-structured-processor.yaml).

> **Important:** the pasted block must start with `transform:`. If you paste only the inner
> `error_mode` / `log_statements` keys, the collector rejects the config with
> `Unavailable Components: processors: [log_statements,error_mode]` and the rollout pauses as *Incompatible*.

## 3. Roll out

1. Click **Compare** and confirm the only changes are the new sources and the processor.
2. **Start Rollout**. The collector restarts briefly, so existing flows may pause for a few seconds.
3. Open **View Configuration File** and check the new pipelines. Each new `logs/s-...` pipeline must export
   to exactly one `forward/...` connector (the raw one to Tenant B, the structured one to Tenant A).

The structured source re-reads every existing build file the first time, so Tenant A fills with history
within a minute or two.

## 4. What the structured lane produces

| Field | Present on | Source |
|---|---|---|
| `timestamp` | every line | the `[...Z]` prefix inside the log |
| `loglevel` (`INFO` / `WARN` / `ERROR`) | every line | pattern match on the message |
| `pipeline.name`, `build.number`, `cicd.tool` | every line | file path |
| `content` | every line | message without the timestamp prefix |
| `pipeline.trigger`, `git.author` | `Started by ...` line | regex |
| `git.commit`, `git.branch` | `Checking out Revision ...` line | regex |
| `git.commit_message` | `Commit message: ...` line | regex |
| `pipeline.stage` | `[Pipeline] { (Build)` marker lines | regex |
| `pipeline.failed_stage` | `Pipeline failed in stage: X` line | regex |
| `pipeline.status` (`SUCCESS` / `FAILURE`) | `Finished: ...` line | regex |

The collector parses each line independently, so fields that appear once per build (commit, status, failed
stage) are **not** copied onto the other lines. Runs are stitched together at query time on
`pipeline.name` + `build.number`; see [`dynatrace-queries.md`](dynatrace-queries.md).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Collector status **Incompatible**, rollout paused | Processor pasted without the `transform:` key (see above) |
| Nothing arrives | Collector user cannot read the directory; check `journalctl -u <collector service>` for "permission denied" |
| Only new builds arrive, first lines of each build missing | `Start At` is `end`; set it to `beginning` |
| `pipeline.name` / `build.number` empty | **Include File Path Attribute** not ticked |
| `timestamp` shows ingestion time, not log time | The time-parsing statement (step 2) failed; the processor ignores errors, so check the collector version supports the OTTL `Time()` function, or use Bindplane's Parse Timestamp processor instead |
| A field is missing but the rest work | The processor uses `error_mode: ignore`, so one failed statement is skipped silently; test statements one at a time |
| Raw and structured data land in the same tenant | A source is connected to both destinations; fix the connections and re-check the generated YAML |
| Facet for `log_type` missing in the Dynatrace Logs app | Display-only; the field is queryable (`filter log_type == "cicd-sdlc"`) |
