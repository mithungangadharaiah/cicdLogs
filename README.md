# SDLC Observability Demo: synthetic CI/CD pipeline logs

A small, dependency-free Python tool that writes **realistic, unstructured Jenkins-style console logs** to disk,
including failures. A log agent (Bindplane / OpenTelemetry Collector) tails the files and ships them to an
observability backend, so you can demonstrate **end-to-end SDLC observability** (build, test, security scan,
deploy, rollback) without access to a customer's real pipelines.

```
 pipeline_log_generator.py          log agent                    observability backend
 ┌──────────────────────┐   files   ┌────────────────┐  raw      ┌──────────────────┐
 │ simulated Jenkins    │ ───────▶  │ Bindplane /    │ ───────▶  │ Tenant B: raw    │
 │ builds, 3 services,  │  jobs/    │ OTel Collector │           │ unstructured logs│
 │ random + scripted    │  <job>/   │ (2 lanes)      │ parsed    ├──────────────────┤
 │ failures             │  builds/  │                │ ───────▶  │ Tenant A:        │
 └──────────────────────┘  <n>/log  └────────────────┘           │ structured logs  │
                                                                 └──────────────────┘
```

> **Status: prototype.** Built for demos and proof-of-concept work. The generator and its tests are stable;
> the Bindplane processor and DQL queries are written for a BDOT 2.0 collector and the queries are not yet
> verified against a live tenant. See [Known limitations](#known-limitations).

The generator itself makes **no network calls**. All shipping and parsing is done by the agent, which is
exactly what you would configure for a real customer.

> **Everything here is synthetic.** Service names, repositories, registries, and CVE ids are fictional.
> Present the output as "what the data looks like once your pipeline is integrated", not as the customer's logs.

## What it simulates

- **Three independent services** (`portal-ui`, `payment-api`, `claims-service`), each with its own pipeline and build counter.
- **Seven stages** per build: Checkout, Build (Maven), Unit Test (Maven Surefire), Security Scan (Trivy), Deploy QA (Helm, kubectl), E2E Test (WebdriverIO), Deploy Prod (Helm, canary analysis).
- **Realistic noise:** dependency download lines, warnings, tool banners, multi-line stack traces.
- **Live streaming:** each build's log file is created when the build starts and appended to line by line, as Jenkins does.
- **Six failure types** and **six scripted scenarios** (bad commit, critical CVE, prod rollback, broken build, test regression, flaky QA deploy). See [docs/scenarios.md](docs/scenarios.md).

### Output layout

```
<base-dir>/jobs/<job-name>/builds/<build-number>/log
<base-dir>/_scenarios/manifest.jsonl        # which builds belong to which scenario (not ingested)
```

Every line has a Timestamper-style prefix:

```
[2026-10-08T10:14:48.605Z] Tests:	 51 passed, 7 failed, 58 total
[2026-10-08T10:14:48.790Z] ERROR: script returned exit code 1
```

The job name and build number exist **only in the file path**, and the commit SHA, stage and final status
each appear on a single line. That is how real Jenkins logs look, and it is why the structured lane needs
parsing plus a query-time join.

## End-to-end steps

1. **Prepare a Linux host** (or VM) with Python 3.8+ and a Bindplane-managed collector installed on it.
2. **Clone** this repo onto the host (see Quick start).
3. **Start the generator** using one of the [run options](#ways-to-run-the-generator) below.
4. **Make the logs readable** by the collector's service user (execute bit on every parent directory, read on the files).
5. **Configure Bindplane**: add the raw and structured File sources and the custom processor, then roll out.
   Full walkthrough with every setting: [docs/bindplane-setup.md](docs/bindplane-setup.md).
6. **Verify** logs arrive in both destinations (raw lane unparsed, structured lane with `pipeline.*` / `git.*` fields).
7. **Demo**: trigger a [scripted scenario](docs/scenarios.md) live and query it with [docs/dynatrace-queries.md](docs/dynatrace-queries.md).

## Quick start

Requirements: Python 3.8+, nothing else.

```bash
git clone <your-repo-url> && cd sdlc-observability-demo

# Smoke test: one build per job, compressed 30x (about 30 seconds)
python3 pipeline_log_generator.py --base-dir /tmp/pipeline-logs --speed 30 --max-runs 1

# Real time, runs forever, ~12% of builds fail
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs

# Only green builds
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs --failure-rate 0

# Play a scripted story, then exit
python3 pipeline_log_generator.py --base-dir /data/CICDPipeLogs/pipeline-logs --scenario bad-commit
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `--base-dir` | `/data/pipeline-logs` | Root directory for the generated files |
| `--jobs` | all three | Subset of jobs to simulate |
| `--speed N` | `1` | Compress durations by N (`1` = real time, a build takes roughly 6 to 13 minutes). Timestamps stay wall-clock and consistent |
| `--interval S` | `120` | Average seconds between builds of the same job |
| `--max-runs N` | `0` | Builds per job in background mode (`0` = forever) |
| `--failure-rate F` | `0.12` | Fraction of background builds that fail |
| `--scenario NAME...` | none | Play scripted scenario(s) and exit; `all` runs every one |
| `--scenario-gap S` | `20` | Real seconds between builds inside a scenario |
| `--list-scenarios` | | Print the scenarios and exit |

### Ways to run the generator

| Mode | Command | Use when |
|---|---|---|
| Smoke test | `python3 pipeline_log_generator.py --base-dir /tmp/pl --speed 30 --max-runs 1` | Checking the output format quickly (about 30 s) |
| Foreground, real time | `python3 pipeline_log_generator.py --base-dir <dir>` | Watching it live in a terminal |
| Background, no service | `nohup python3 pipeline_log_generator.py --base-dir <dir> > gen.out 2>&1 &` | Quick demo, no root access |
| systemd service | see below | Long-lived demo environment; survives logout, crash, reboot |
| Scripted scenario | `python3 pipeline_log_generator.py --base-dir <dir> --scenario critical-cve` | Live storytelling; runs and exits, can run next to the service |
| Subset of services | `--jobs payment-api portal-ui` | Smaller, focused data set |
| Fast fill | `--speed 10 --interval 30` | Populating the tenant with history before a demo |

Run `python3 pipeline_log_generator.py --list-scenarios` for scenario names.

### Run it as a service

[`deploy/pipeline-log-generator.service`](deploy/pipeline-log-generator.service) is a systemd unit that keeps
the background generator running across session closes, crashes, and reboots. Edit the paths, then:

```bash
sudo cp deploy/pipeline-log-generator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pipeline-log-generator
journalctl -u pipeline-log-generator -f
```

Scenarios are run by hand (they are meant to be triggered live) and can run next to the service.

### Housekeeping

Each build is a new file, so files accumulate (a few hundred per day at default settings). A daily cron keeps it tidy:

```bash
find /data/CICDPipeLogs/pipeline-logs/jobs -name log -mtime +2 -delete
```

## Shipping the logs (Bindplane)

[docs/bindplane-setup.md](docs/bindplane-setup.md) describes a two-lane setup on one collector:

| Lane | Processing | Shows |
|---|---|---|
| Raw | none | The logs exactly as written |
| Structured | [`bindplane/cicd-structured-processor.yaml`](bindplane/cicd-structured-processor.yaml) | Timestamp, severity, pipeline name, build number, commit, branch, author, stage, failed stage, final status |

Two things that bite people: tick **Include File Path Attribute** on the File source, and make sure the custom
processor block starts with `transform:`.

## Querying

[docs/dynatrace-queries.md](docs/dynatrace-queries.md) has DQL for a one-row-per-build view, failure analysis,
and drill-down. Those queries are **not yet verified against a live tenant**; treat them as a starting point.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests run every failure kind and every scenario at very high speed and assert that the output has the
expected structure, status, failed stage, and severity classification, using the same regexes as the Bindplane processor.
If you change a log format or a processor pattern, update both places.

## Repository layout

```
pipeline_log_generator.py                 the generator (single file, stdlib only)
bindplane/cicd-structured-processor.yaml  parsing for the structured lane
deploy/pipeline-log-generator.service     systemd unit for the background generator
docs/bindplane-setup.md                   collector configuration walkthrough
docs/dynatrace-queries.md                 DQL examples
docs/scenarios.md                         failure kinds, scenarios, sample output
tests/test_generator.py                   output and parsing-contract tests
```

## Known limitations

- **Synthetic data.** The application-side impact of a bad release (error-rate metrics, traces) is not
  generated; only the pipeline's own log of it is.
- **Failure details are canned.** Stack traces and messages are templates, not output from real code.
- **Stage fields are not on every line.** Commit, stage, and status are extracted only from the lines that
  contain them; a query-time join on `pipeline.name` + `build.number` stitches a build together.
- **Per-stage duration, retry detection, and DORA-style metrics** are described in the queries doc but not built.
- **Jenkins-only format.** GitLab CI, GitHub Actions, and Azure DevOps logs look different.
- The Bindplane processor and DQL were written for a Bindplane BDOT 2.0 collector and may need adjusting for other versions.

## Security

- No credentials are used or stored by this repository. API tokens for the backend belong in the agent's
  configuration (e.g. Bindplane), never in the repo.
- Treat generated logs and any screenshots of your tenant as internal if they reveal tenant names or hosts.

## License

MIT, see [LICENSE](LICENSE).
