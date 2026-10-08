#!/usr/bin/env python3
"""
Live Jenkins-style pipeline log generator (unstructured text) with failure scenarios.

Writes console logs the way Jenkins does, to:
    <base-dir>/jobs/<job-name>/builds/<build-number>/log

Each log file is APPENDED to line by line while the simulated build runs, so a
file-tailing agent (Bindplane, OTel Collector filelog, Fluent Bit, ...) sees it
grow in real time. Every line carries a Timestamper-style prefix:
    [2026-10-08T10:02:11.482Z] <raw tool output>

No observability backend is called. Python standard library only.

MODES
  Background (default)  Runs forever; each job produces builds at random
                        intervals. --failure-rate controls how many fail.
  Scenario              --scenario NAME [NAME ...] plays a scripted story
                        (e.g. a bad commit failing 3 times, then a fix) and exits.

EXAMPLES
  python3 pipeline_log_generator.py --base-dir /data/pipeline-logs
  python3 pipeline_log_generator.py --failure-rate 0   # successes only
  python3 pipeline_log_generator.py --list-scenarios
  python3 pipeline_log_generator.py --scenario bad-commit --speed 10
  python3 pipeline_log_generator.py --scenario all --scenario-gap 30

--speed N compresses stage durations by N (1 = real time). Timestamps are always
wall-clock and durations printed in the log match them, so data stays consistent.
"""
import argparse
import json
import os
import random
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Job definitions (three independent simulated applications)
# --------------------------------------------------------------------------
JOBS = {
    "portal-ui-ci-cd": {
        "repo": "customer-portal/portal-ui",
        "artifact": "portal-ui",
        "pkg": "com.corp.portal",
        "components": ["checkout", "cart", "account", "policy", "search"],
        "e2e_suite": "checkout",
        "e2e_selector": "#pay-now-button",
        "endpoint": "/checkout/confirm",
        "e2e_tests": [
            "should render the cart summary",
            "should apply a discount code",
            "should complete payment with saved card",
            "should show validation errors on empty form",
            "should download policy document",
            "should update billing address",
            "should display premium quote",
            "should log in with valid credentials",
            "should log out and clear session",
            "should renew an active policy",
        ],
    },
    "payment-api-ci-cd": {
        "repo": "payments/payment-api",
        "artifact": "payment-api",
        "pkg": "com.corp.payments",
        "components": ["gateway", "ledger", "refund", "invoice", "fraud"],
        "e2e_suite": "payments",
        "e2e_selector": "#authorize-payment",
        "endpoint": "/v1/payments",
        "e2e_tests": [
            "should authorize a card payment",
            "should reject an expired card",
            "should process a full refund",
            "should process a partial refund",
            "should generate an invoice",
            "should retry on gateway timeout",
            "should flag a suspicious transaction",
            "should reconcile daily settlements",
        ],
    },
    "claims-service-ci-cd": {
        "repo": "claims/claims-service",
        "artifact": "claims-service",
        "pkg": "com.corp.claims",
        "components": ["intake", "assessment", "settlement", "documents", "workflow"],
        "e2e_suite": "claims",
        "e2e_selector": "#submit-claim-button",
        "endpoint": "/v1/claims",
        "e2e_tests": [
            "should submit a new claim",
            "should upload supporting documents",
            "should assign an adjuster",
            "should calculate settlement amount",
            "should reject a duplicate claim",
            "should notify the customer on approval",
            "should escalate a high-value claim",
            "should close a settled claim",
        ],
    },
}

AUTHORS = ["asha-k", "rahul-m", "priya-s", "john-d", "mei-l"]
COMMIT_MSGS = [
    "Refactor validation logic", "Fix null check in mapper", "Add retry logic to client",
    "Update dependency versions", "Improve error handling", "Add unit tests for service layer",
    "Optimize database query", "Update API response model", "Clean up logging statements",
    "Add pagination to list endpoint", "Fix date formatting issue", "Rename config properties",
]
CLASS_SUFFIXES = ["Validator", "Service", "Controller", "Mapper", "Repository", "Handler", "Client"]
ARTIFACTS = [
    ("org/springframework/boot/spring-boot-starter-web/3.2.4", "spring-boot-starter-web-3.2.4"),
    ("org/springframework/boot/spring-boot-starter-data-jpa/3.2.4", "spring-boot-starter-data-jpa-3.2.4"),
    ("com/fasterxml/jackson/core/jackson-databind/2.16.1", "jackson-databind-2.16.1"),
    ("org/apache/commons/commons-lang3/3.14.0", "commons-lang3-3.14.0"),
    ("org/hibernate/orm/hibernate-core/6.4.4.Final", "hibernate-core-6.4.4.Final"),
    ("io/micrometer/micrometer-core/1.12.4", "micrometer-core-1.12.4"),
    ("org/postgresql/postgresql/42.7.2", "postgresql-42.7.2"),
    ("ch/qos/logback/logback-classic/1.4.14", "logback-classic-1.4.14"),
    ("org/slf4j/slf4j-api/2.0.12", "slf4j-api-2.0.12"),
    ("io/netty/netty-handler/4.1.107.Final", "netty-handler-4.1.107.Final"),
    ("org/junit/jupiter/junit-jupiter-api/5.10.2", "junit-jupiter-api-5.10.2"),
    ("org/mockito/mockito-core/5.11.0", "mockito-core-5.11.0"),
    ("com/google/guava/guava/33.0.0-jre", "guava-33.0.0-jre"),
    ("org/apache/tomcat/embed/tomcat-embed-core/10.1.19", "tomcat-embed-core-10.1.19"),
    ("com/zaxxer/HikariCP/5.1.0", "HikariCP-5.1.0"),
]
BUILD_WARNINGS = [
    "[WARNING] The POM for com.corp:legacy-utils:jar:1.0.4 is missing, no dependency information available",
    "[WARNING] Parameter 'forkCount' is deprecated, use 'forkedProcessExitTimeoutInSeconds' instead",
    "[WARNING] /src/main/java/{pkg_path}/util/DateHelper.java:[42,17] unchecked call to add(E) as a member of raw type java.util.List",
    "[WARNING] Using platform encoding (ANSI_X3.4-1968 actually) to copy filtered resources, i.e. build is platform dependent!",
    "[WARNING] The following patterns were never triggered in this artifact inclusion filter: org/slf4j/**",
]
# Fictional CVE ids and libraries (not real vulnerabilities).
CVE_LIBS = [
    {"lib": "com.corp.thirdparty:document-parser", "id": "CVE-2026-99001", "installed": "2.3.1", "fixed": "2.3.4"},
    {"lib": "org.example.xml:xml-processor", "id": "CVE-2026-99014", "installed": "1.8.0", "fixed": "1.8.3"},
    {"lib": "io.example.net:http-codec", "id": "CVE-2026-99027", "installed": "4.1.9", "fixed": "4.1.12"},
]
CHROME = "chrome 124.0.6367.91 linux"

# Failure kinds -> the stage they break, and relative frequency in background mode.
FAILURE_STAGE = {
    "compile-error": "Build",
    "unit-test-failure": "Unit Test",
    "cve-critical": "Security Scan",
    "deploy-qa-failure": "Deploy QA",
    "e2e-failure": "E2E Test",
    "prod-rollback": "Deploy Prod",
}
FAILURE_WEIGHTS = {
    "compile-error": 2, "unit-test-failure": 3, "cve-critical": 2,
    "deploy-qa-failure": 1, "e2e-failure": 4, "prod-rollback": 1,
}

# --------------------------------------------------------------------------
# Scripted scenarios (each is a short story played as consecutive builds)
# --------------------------------------------------------------------------
SCENARIOS = {
    "bad-commit": {
        "title": "Bad commit breaks checkout E2E tests; rebuilt twice; then fixed",
        "job": "portal-ui-ci-cd", "branch": "main", "author": "rahul-m",
        "commits": {"bad": "Refactor checkout payment form", "fix": "Fix: restore pay-now button id"},
        "steps": [
            {"commit": "bad", "fail": "e2e-failure"},
            {"commit": "bad", "fail": "e2e-failure", "started_by": "user rahul-m"},
            {"commit": "bad", "fail": "e2e-failure", "started_by": "user rahul-m"},
            {"commit": "fix", "fail": None},
        ],
    },
    "cve-blocked": {
        "title": "Critical CVE found by the security scan blocks the release; fixed by a dependency bump",
        "job": "claims-service-ci-cd", "branch": "main", "author": "priya-s",
        "commits": {"bad": "Upgrade document-parser library to 2.3.1", "fix": "Bump document-parser to 2.3.4 (CVE fix)"},
        "cve": CVE_LIBS[0],
        "steps": [
            {"commit": "bad", "fail": "cve-critical"},
            {"commit": "fix", "fail": None},
        ],
    },
    "prod-rollback": {
        "title": "Release passes every test gate, then canary analysis fails in prod and rolls back",
        "job": "payment-api-ci-cd", "branch": "main", "author": "asha-k",
        "commits": {"bad": "Add retry logic to gateway client", "fix": "Fix retry backoff in gateway client"},
        "steps": [
            {"commit": "bad", "fail": "prod-rollback"},
            {"commit": "fix", "fail": None},
        ],
    },
    "broken-build": {
        "title": "Compilation error; a quick fix also fails; third attempt is green",
        "job": "claims-service-ci-cd", "branch": "develop", "author": "john-d",
        "commits": {"bad": "Add settlement calculator", "bad2": "Quick fix for settlement calculator", "fix": "Fix settlement calculator API usage"},
        "steps": [
            {"commit": "bad", "fail": "compile-error"},
            {"commit": "bad2", "fail": "compile-error"},
            {"commit": "fix", "fail": None},
        ],
    },
    "test-regression": {
        "title": "Unit test regression introduced by a refactor; fixed in the next commit",
        "job": "payment-api-ci-cd", "branch": "develop", "author": "mei-l",
        "commits": {"bad": "Refactor refund amount rounding", "fix": "Fix rounding regression in refunds"},
        "steps": [
            {"commit": "bad", "fail": "unit-test-failure"},
            {"commit": "fix", "fail": None},
        ],
    },
    "flaky-qa-deploy": {
        "title": "QA deployment fails (infrastructure flake); a retry of the same commit succeeds",
        "job": "portal-ui-ci-cd", "branch": "main", "author": "mei-l",
        "commits": {"c": "Update API response model"},
        "steps": [
            {"commit": "c", "fail": "deploy-qa-failure"},
            {"commit": "c", "fail": None, "started_by": "user mei-l"},
        ],
    },
}

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def now_ts():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def fmt_mvn(sec):
    if sec >= 60:
        return f"{int(sec // 60):02d}:{int(sec % 60):02d} min"
    return f"{sec:.3f} s"


def fmt_hms(sec):
    return f"{int(sec // 3600):02d}:{int((sec % 3600) // 60):02d}:{int(sec % 60):02d}"


def split_total(total, parts):
    """Split `total` into `parts` positive integers that sum to total."""
    parts = max(1, min(parts, total))
    cuts = sorted(random.sample(range(1, total), parts - 1)) if parts > 1 else []
    edges = [0] + cuts + [total]
    return [edges[i + 1] - edges[i] for i in range(parts)]


def box_table(header, rows):
    """Render a Trivy-style unicode table."""
    cols = list(zip(*([header] + rows)))
    widths = [max(len(c) for c in col) + 2 for col in cols]

    def rule(l, m, r):
        return l + m.join("\u2500" * w for w in widths) + r

    def row(cells):
        return "\u2502" + "\u2502".join(" " + c.ljust(w - 1) for c, w in zip(cells, widths)) + "\u2502"

    return [rule("\u250c", "\u252c", "\u2510"), row(header), rule("\u251c", "\u253c", "\u2524")] + \
           [row(r) for r in rows] + [rule("\u2514", "\u2534", "\u2518")]


class PipelineFailure(Exception):
    def __init__(self, stage, kind):
        super().__init__(f"{stage}: {kind}")
        self.stage = stage
        self.kind = kind


class Writer:
    """Appends timestamped lines to a file, sleeping between lines like a real build."""

    def __init__(self, fh, speed):
        self.fh = fh
        self.speed = speed

    def line(self, msg=""):
        msg = msg.replace("{now}", now_ts())  # placeholder for tools that print their own time
        self.fh.write(f"[{now_ts()}] {msg}\n")
        self.fh.flush()

    def stream(self, lines, total_secs):
        """Emit lines spread randomly over roughly total_secs (scaled by speed)."""
        if not lines:
            return
        total = total_secs / self.speed
        weights = [random.random() + 0.05 for _ in lines]
        scale = total / sum(weights)
        for msg, wt in zip(lines, weights):
            self.line(msg)
            time.sleep(wt * scale)


def reserve_build(base, job):
    """Atomically claim the next build number by creating its directory."""
    d = os.path.join(base, "jobs", job, "builds")
    os.makedirs(d, exist_ok=True)
    nums = [int(x) for x in os.listdir(d) if x.isdigit()]
    n = (max(nums) + 1) if nums else 1000 + random.randint(1, 50)
    while True:
        p = os.path.join(d, str(n))
        try:
            os.mkdir(p)
            return n, p
        except FileExistsError:
            n += 1


# --------------------------------------------------------------------------
# Stage plumbing
# --------------------------------------------------------------------------


def stage_open(w, name):
    w.line("[Pipeline] stage")
    w.line(f"[Pipeline] {{ ({name})")


def stage_close(w):
    w.line("[Pipeline] }")
    w.line("[Pipeline] // stage")


def stage_skipped(w, name, reason):
    w.line("[Pipeline] stage")
    w.line(f"[Pipeline] {{ ({name})")
    w.line(f'Stage "{name}" skipped due to {reason}')
    stage_close(w)


def fail_exit(w, stage, kind, code=1):
    w.line(f"ERROR: script returned exit code {code}")
    stage_close(w)
    raise PipelineFailure(stage, kind)


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def stage_checkout(w, cfg, ctx):
    stage_open(w, "Checkout")
    url = f"https://git.corp.example.com/{cfg['repo']}.git"
    w.stream([
        f"Cloning repository {url}",
        f" > git init /var/lib/jenkins/workspace/{ctx['job']} # timeout=10",
        f"Fetching upstream changes from {url}",
        f" > git fetch --tags --force --progress -- {url} +refs/heads/*:refs/remotes/origin/* # timeout=10",
        f"Checking out Revision {ctx['sha']} (origin/{ctx['branch']})",
        f" > git checkout -f {ctx['sha']} # timeout=10",
        f'Commit message: "{ctx["msg"]}"',
    ], random.randint(4, 9))
    stage_close(w)


def maven_start(cfg, ctx, n_deps):
    lines = ["+ mvn clean package -DskipTests", "[INFO] Scanning for projects...",
             f"[INFO] Building {cfg['artifact']} {ctx['version']}"]
    for path, name in random.sample(ARTIFACTS, n_deps):
        url = f"https://repo.maven.apache.org/maven2/{path}/{name}.pom"
        lines.append(f"[INFO] Downloading from central: {url}")
        lines.append(f"[INFO] Downloaded from central: {url} ({random.uniform(1, 40):.1f} kB at {random.randint(10, 400)} kB/s)")
    return lines


def stage_build(w, cfg, ctx):
    stage_open(w, "Build")
    t0 = time.time()
    art = cfg["artifact"]
    pkg_path = cfg["pkg"].replace(".", "/")
    failing = ctx["fail"] == "compile-error"
    lines = maven_start(cfg, ctx, random.randint(4, 8) if failing else random.randint(8, 14))
    if not failing:
        for wmsg in random.sample(BUILD_WARNINGS, random.randint(1, 3)):
            lines.append(wmsg.format(pkg_path=pkg_path))
    lines += [
        f"[INFO] --- maven-resources-plugin:3.3.1:resources (default-resources) @ {art} ---",
        f"[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) @ {art} ---",
        f"[INFO] Compiling {random.randint(120, 480)} source files with javac [debug release 17] to target/classes",
    ]
    if failing:
        frng = ctx["frng"]("compile")
        comp = frng.choice(cfg["components"])
        cls = comp.capitalize() + frng.choice(["Service", "Controller", "Handler"])
        src = f"/var/lib/jenkins/workspace/{ctx['job']}/src/main/java/{pkg_path}/{comp}/{cls}.java"
        pos = f"[{frng.randint(30, 200)},{frng.randint(10, 60)}]"
        sym = frng.choice(["getPaymentToken()", "resolvePolicyId()", "applyDiscount(java.lang.String)"])
        w.stream(lines, ctx["durations"]["build"] * random.uniform(0.3, 0.6))
        el = time.time() - t0
        w.stream([
            "[INFO] -------------------------------------------------------------",
            "[ERROR] COMPILATION ERROR : ",
            "[INFO] -------------------------------------------------------------",
            f"[ERROR] {src}:{pos} cannot find symbol",
            f"[ERROR]   symbol:   method {sym}",
            f"[ERROR]   location: variable request of type {cfg['pkg']}.{comp}.Request",
            "[INFO] 1 error",
            "[INFO] -------------------------------------------------------------",
            "[INFO] ------------------------------------------------------------------------",
            "[INFO] BUILD FAILURE",
            "[INFO] ------------------------------------------------------------------------",
            f"[INFO] Total time:  {fmt_mvn(el)}",
            "[INFO] Finished at: {now}",
            f"[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11.0:compile (default-compile) on project {art}: Compilation failure",
            f"[ERROR] {src}:{pos} cannot find symbol",
            f"[ERROR]   symbol:   method {sym}",
            "[ERROR] -> [Help 1]",
        ], 1)
        fail_exit(w, "Build", "compile-error")
    lines += [
        f"[INFO] --- maven-jar-plugin:3.3.0:jar (default-jar) @ {art} ---",
        f"[INFO] Building jar: /var/lib/jenkins/workspace/{ctx['job']}/target/{art}-{ctx['version']}.jar",
        f"[INFO] --- spring-boot-maven-plugin:3.2.4:repackage (repackage) @ {art} ---",
    ]
    w.stream(lines, ctx["durations"]["build"])
    el = time.time() - t0
    w.stream([
        "[INFO] ------------------------------------------------------------------------",
        "[INFO] BUILD SUCCESS",
        "[INFO] ------------------------------------------------------------------------",
        f"[INFO] Total time:  {fmt_mvn(el)}",
        "[INFO] Finished at: {now}",
    ], 1)
    stage_close(w)


def stage_unit_test(w, cfg, ctx):
    stage_open(w, "Unit Test")
    failing = ctx["fail"] == "unit-test-failure"
    art = cfg["artifact"]
    total_tests = random.randint(250, 600)
    n_classes = random.randint(14, 24)
    counts = split_total(total_tests, n_classes)
    classes = set()
    while len(classes) < n_classes:
        classes.add(f"{cfg['pkg']}.{random.choice(cfg['components'])}.{random.choice(cfg['components']).capitalize()}{random.choice(CLASS_SUFFIXES)}Test")
    classes = sorted(classes)
    lines = [
        "+ mvn test",
        "[INFO] -------------------------------------------------------",
        "[INFO]  T E S T S",
        "[INFO] -------------------------------------------------------",
    ]

    def ok(cls, cnt):
        return [f"[INFO] Running {cls}",
                f"[INFO] Tests run: {cnt}, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: {random.uniform(0.2, 6):.3f} s - in {cls}"]

    if not failing:
        for cls, cnt in zip(classes, counts):
            lines += ok(cls, cnt)
        lines += ["[INFO] ", "[INFO] Results:", "[INFO] ",
                  f"[INFO] Tests run: {total_tests}, Failures: 0, Errors: 0, Skipped: {random.randint(0, 4)}",
                  "[INFO] ", "[INFO] BUILD SUCCESS"]
        w.stream(lines, ctx["durations"]["unit"])
        stage_close(w)
        return

    frng = ctx["frng"]("unit")
    fi = frng.randint(max(1, n_classes // 3), n_classes - 2)
    for cls, cnt in zip(classes[:fi], counts[:fi]):
        lines += ok(cls, cnt)
    bad, bad_cnt = classes[fi], counts[fi]
    pkg_of_bad = bad.rsplit(".", 1)[0]
    impl = bad.rsplit(".", 1)[1][:-4]
    method = frng.choice(["shouldCalculatePremium", "shouldValidateEmptyAddress", "shouldRejectDuplicateClaim", "shouldApplyDiscount"])
    variant = frng.choice(["npe", "assert"])
    tline, iline = frng.randint(30, 120), frng.randint(20, 200)
    f_cnt, e_cnt = (0, 1) if variant == "npe" else (1, 0)
    lines.append(f"[INFO] Running {bad}")
    lines.append(f"[ERROR] Tests run: {bad_cnt}, Failures: {f_cnt}, Errors: {e_cnt}, Skipped: 0, Time elapsed: {random.uniform(0.3, 2):.3f} s <<< FAILURE! - in {bad}")
    lines.append(f"[ERROR] {method}  Time elapsed: {random.uniform(0.01, 0.2):.3f} s  <<< {'ERROR' if variant == 'npe' else 'FAILURE'}!")
    if variant == "npe":
        msg = 'Cannot invoke "String.trim()" because "address.line1" is null'
        lines += [
            f"java.lang.NullPointerException: {msg}",
            f"    at {pkg_of_bad}.{impl}.validate({impl}.java:{iline})",
            f"    at {bad}.{method}({impl}Test.java:{tline})",
            "    at java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103)",
            "    at java.base/java.lang.reflect.Method.invoke(Method.java:580)",
            "    at java.base/java.util.ArrayList.forEach(ArrayList.java:1596)",
        ]
        summary = [f"[ERROR] Errors: ", f"[ERROR]   {impl}Test.{method}:{tline} \u00bb NullPointer {msg}"]
    else:
        exp, got = frng.choice([("1250.00", "1187.50"), ("310.50", "310.00"), ("89.99", "90.00")])
        msg = f"expected: <{exp}> but was: <{got}>"
        lines += [
            f"org.opentest4j.AssertionFailedError: {msg}",
            "    at org.junit.jupiter.api.AssertionFailureBuilder.build(AssertionFailureBuilder.java:151)",
            "    at org.junit.jupiter.api.AssertEquals.assertEquals(AssertEquals.java:145)",
            f"    at {bad}.{method}({impl}Test.java:{tline})",
            "    at java.base/java.lang.reflect.Method.invoke(Method.java:580)",
        ]
        summary = [f"[ERROR] Failures: ", f"[ERROR]   {impl}Test.{method}:{tline} {msg}"]
    done = sum(counts[:fi + 1])
    lines += ["[INFO] ", "[INFO] Results:", "[INFO] "] + summary + [
        "[INFO] ",
        f"[ERROR] Tests run: {done}, Failures: {f_cnt}, Errors: {e_cnt}, Skipped: 0",
        "[INFO] ",
        "[INFO] ------------------------------------------------------------------------",
        "[INFO] BUILD FAILURE",
        "[INFO] ------------------------------------------------------------------------",
        f"[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.2.5:test (default-test) on project {art}: There are test failures.",
        "[ERROR] -> [Help 1]",
    ]
    w.stream(lines, ctx["durations"]["unit"] * random.uniform(0.4, 0.8))
    fail_exit(w, "Unit Test", "unit-test-failure")


def stage_security(w, cfg, ctx):
    stage_open(w, "Security Scan")
    failing = ctx["fail"] == "cve-critical"
    image = f"registry.corp.example.com/{cfg['artifact']}:{ctx['sha'][:7]}"
    lines = [
        f"+ trivy image --severity HIGH,CRITICAL --exit-code 1 {image}",
        "{now}\tINFO\tVulnerability scanning is enabled",
        "{now}\tINFO\tSecret scanning is enabled",
        "{now}\tINFO\tIf your scanning is slow, please try '--scanners vuln' to disable secret scanning",
        "{now}\tINFO\tDetected OS: debian",
        "{now}\tINFO\tDetecting Debian vulnerabilities...",
        f"{{now}}\tINFO\tNumber of language-specific files: {random.randint(1, 3)}",
        "{now}\tINFO\t[jar] Detecting vulnerabilities...",
        "",
        f"{image} (debian 12.5)",
        "=" * (len(image) + 15),
        "Total: 0 (HIGH: 0, CRITICAL: 0)",
        "",
        "Java (jar)",
        "==========",
    ]
    if not failing:
        lines.append("Total: 0 (HIGH: 0, CRITICAL: 0)")
        w.stream(lines, random.randint(60, 200))
        stage_close(w)
        return
    cve = ctx.get("cve") or ctx["frng"]("cve").choice(CVE_LIBS)
    lines += ["Total: 1 (HIGH: 0, CRITICAL: 1)", ""]
    lines += box_table(
        ["Library", "Vulnerability", "Severity", "Status", "Installed Version", "Fixed Version"],
        [[cve["lib"], cve["id"], "CRITICAL", "fixed", cve["installed"], cve["fixed"]]])
    lines += ["", f"Security gate FAILED: 1 critical vulnerability exceeds policy threshold (max allowed: 0)"]
    w.stream(lines, random.randint(60, 200) * random.uniform(0.6, 1.0))
    fail_exit(w, "Security Scan", "cve-critical")


def helm_lines(cfg, ctx, env, revision):
    art = cfg["artifact"]
    ts = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
    return [
        f"+ helm upgrade --install {art} ./chart --namespace {env} --set image.tag={ctx['sha'][:7]} --wait --timeout 120s",
        f'Release "{art}" has been upgraded. Happy Helming!',
        f"NAME: {art}",
        f"LAST DEPLOYED: {ts}",
        f"NAMESPACE: {env}",
        "STATUS: deployed",
        f"REVISION: {revision}",
        f"+ kubectl rollout status deployment/{art} -n {env} --timeout=120s",
        f'Waiting for deployment "{art}" rollout to finish: 1 out of 3 new replicas have been updated...',
        f'Waiting for deployment "{art}" rollout to finish: 2 of 3 updated replicas are available...',
        f'deployment "{art}" successfully rolled out',
    ]


def stage_deploy_qa(w, cfg, ctx):
    stage_open(w, "Deploy QA")
    art = cfg["artifact"]
    if ctx["fail"] != "deploy-qa-failure":
        w.stream(helm_lines(cfg, ctx, "qa", random.randint(100, 400)), ctx["durations"]["qa"])
        stage_close(w)
        return
    frng = ctx["frng"]("qa")
    variant = frng.choice(["imagepull", "crashloop"])
    pod = f"{art}-{uuid.uuid4().hex[:9]}-{uuid.uuid4().hex[:5]}"
    img = f"registry.corp.example.com/{art}:{ctx['sha'][:7]}"
    lines = [
        f"+ helm upgrade --install {art} ./chart --namespace qa --set image.tag={ctx['sha'][:7]} --wait --timeout 120s",
        "Error: UPGRADE FAILED: timed out waiting for the condition",
        f"+ kubectl get pods -n qa -l app={art}",
        "NAME                              READY   STATUS             RESTARTS   AGE",
    ]
    if variant == "imagepull":
        lines += [
            f"{pod}   0/1     ImagePullBackOff   0          2m",
            f"{art}-5c7d8f6b4-q9w8e            1/1     Running            0          3d",
            f"+ kubectl describe pod {pod} -n qa",
            "Events:",
            "  Type     Reason   Age   From     Message",
            "  ----     ------   ----  ----     -------",
            f'  Warning  Failed   2m    kubelet  Failed to pull image "{img}": rpc error: code = NotFound desc = failed to pull and unpack image: not found',
            "  Warning  Failed   2m    kubelet  Error: ErrImagePull",
            f'  Normal   BackOff  90s   kubelet  Back-off pulling image "{img}"',
            "  Warning  Failed   90s   kubelet  Error: ImagePullBackOff",
        ]
    else:
        lines += [
            f"{pod}   0/1     CrashLoopBackOff   4          2m",
            f"{art}-5c7d8f6b4-q9w8e            1/1     Running            0          3d",
            f"+ kubectl describe pod {pod} -n qa",
            "Events:",
            "  Type     Reason     Age   From     Message",
            "  ----     ------     ----  ----     -------",
            "  Warning  Unhealthy  1m    kubelet  Readiness probe failed: HTTP probe failed with statuscode: 503",
            "  Warning  BackOff    30s   kubelet  Back-off restarting failed container",
        ]
    w.stream(lines, ctx["durations"]["qa"] * random.uniform(0.7, 1.2))
    fail_exit(w, "Deploy QA", "deploy-qa-failure")


def stage_e2e(w, cfg, ctx):
    stage_open(w, "E2E Test")
    tests = cfg["e2e_tests"]
    suite = cfg["e2e_suite"]
    failing = ctx["fail"] == "e2e-failure"
    specs = random.randint(6, 12)
    n = random.randint(len(tests), len(tests) * 4)
    workers = random.randint(3, 6)
    lines = [f"+ npx wdio run wdio.conf.js --suite {suite}", f"Execution of {specs} workers started at {{now}}"]
    for i in range(workers):
        lines.append(f"[{CHROME} #0-{i}] Running: chrome (v124.0.6367.91) on linux")
        lines.append(f"[{CHROME} #0-{i}] Session ID: {uuid.uuid4().hex}")
    if not failing:
        for i in range(n):
            lines.append(f"[{CHROME} #0-{i % workers}] \u2713 {tests[i % len(tests)]} ({random.uniform(0.6, 9):.1f}s)")
        w.stream(lines, ctx["durations"]["e2e"])
        w.stream([
            f"Spec Files:\t {specs} passed, {specs} total (100% completed) in {fmt_hms(ctx['durations']['e2e'] / w.speed)}",
            f"Tests:\t {n} passed, {n} total",
        ], 1)
        stage_close(w)
        return

    frng = ctx["frng"]("e2e")
    nfail = frng.randint(2, min(7, len(tests)))
    titles = frng.sample(tests, nfail)
    sel = cfg["e2e_selector"]
    n_pass = n - nfail
    items = [("pass", tests[i % len(tests)]) for i in range(n_pass)] + [("fail", t) for t in titles]
    random.shuffle(items)
    for i, (kind, title) in enumerate(items):
        mark = "\u2713" if kind == "pass" else "\u2716"
        lines.append(f"[{CHROME} #0-{i % workers}] {mark} {title} ({random.uniform(0.6, 9):.1f}s)")
    w.stream(lines, ctx["durations"]["e2e"] * random.uniform(0.5, 0.8))
    details = []
    for idx, title in enumerate(titles, 1):
        k = f"[{CHROME} #0-{idx % workers}]"
        spec_line = frng.randint(40, 140)
        details += [
            f"{k} ",
            f"{k} {idx}) {suite} {title}",
            f'{k} element ("{sel}") still not displayed after 10000ms',
            f'{k} Error: element ("{sel}") still not displayed after 10000ms',
            f"    at Element.waitForDisplayed (/var/lib/jenkins/workspace/{ctx['job']}/node_modules/webdriverio/build/commands/element/waitForDisplayed.js:41:15)",
            f"    at async Context.<anonymous> (/var/lib/jenkins/workspace/{ctx['job']}/test/e2e/{suite}.spec.js:{spec_line}:9)",
            f"    at async Context.executeAsync (/var/lib/jenkins/workspace/{ctx['job']}/node_modules/@wdio/utils/build/shim.js:331:28)",
        ]
    f_specs = min(specs - 1, frng.randint(1, 3))
    details += [
        f"Spec Files:\t {specs - f_specs} passed, {f_specs} failed, {specs} total (100% completed) in {fmt_hms(ctx['durations']['e2e'] * 0.65 / w.speed)}",
        f"Tests:\t {n_pass} passed, {nfail} failed, {n} total",
    ]
    w.stream(details, 2)
    fail_exit(w, "E2E Test", "e2e-failure")


def stage_deploy_prod(w, cfg, ctx):
    stage_open(w, "Deploy Prod")
    art = cfg["artifact"]
    rev = random.randint(100, 400)
    if ctx["fail"] != "prod-rollback":
        w.stream(helm_lines(cfg, ctx, "prod", rev), ctx["durations"]["prod"])
        stage_close(w)
        return
    frng = ctx["frng"]("prod")
    mid = round(frng.uniform(1.5, 3.5), 1)
    peak = round(frng.uniform(6.0, 12.0), 1)
    fails = frng.randint(80, 260)
    lines = helm_lines(cfg, ctx, "prod", rev) + [
        f"+ ./scripts/canary-analysis.sh {art} --duration 5m --max-error-rate 5.0",
        f"Canary analysis: baseline error rate 0.3%, canary error rate {mid}%",
        f"WARNING: canary error rate {mid}% is above baseline 0.3%",
        f"Canary analysis: baseline error rate 0.3%, canary error rate {peak}%",
        f"Error: {fails} requests to {cfg['endpoint']} failed in the last 2 minutes (HTTP 504 Gateway Timeout)",
        f"ERROR: canary analysis failed: error rate {peak}% exceeds threshold 5.0%",
        f"+ helm rollback {art} {rev - 1} --namespace prod --wait",
        "Rollback was a success! Happy Helming!",
        "Canary analysis: error rate back to 0.3% after rollback",
    ]
    w.stream(lines, ctx["durations"]["prod"] * random.uniform(1.5, 2.5))
    fail_exit(w, "Deploy Prod", "prod-rollback")


STAGES = [
    ("Checkout", stage_checkout),
    ("Build", stage_build),
    ("Unit Test", stage_unit_test),
    ("Security Scan", stage_security),
    ("Deploy QA", stage_deploy_qa),
    ("E2E Test", stage_e2e),
    ("Deploy Prod", stage_deploy_prod),
]

# --------------------------------------------------------------------------
# One full pipeline run
# --------------------------------------------------------------------------


def pick_failure():
    kinds = list(FAILURE_WEIGHTS)
    return random.choices(kinds, weights=[FAILURE_WEIGHTS[k] for k in kinds])[0]


def run_pipeline(base, job, cfg, speed, fail=None, overrides=None):
    """Simulate one build. `fail` is a key of FAILURE_STAGE or None. Returns a result dict."""
    ov = overrides or {}
    build_no, log_dir = reserve_build(base, job)
    path = os.path.join(log_dir, "log")

    branch = ov.get("branch") or random.choices(
        ["main", "develop", f"feature/{random.randint(100, 999)}"], weights=[6, 2, 2])[0]
    if fail == "prod-rollback":
        branch = "main"  # Deploy Prod only runs on main
    author = ov.get("author") or random.choice(AUTHORS)
    sha = ov.get("sha") or (uuid.uuid4().hex + uuid.uuid4().hex[:8])
    vr = random.Random(sha)
    ctx = {
        "job": job,
        "build_no": build_no,
        "branch": branch,
        "sha": sha,
        "msg": ov.get("msg") or random.choice(COMMIT_MSGS),
        "author": author,
        "started_by": ov.get("started_by") or f"GitHub push by {author}",
        "cve": ov.get("cve"),
        "fail": fail,
        "version": f"1.{vr.randint(2, 9)}.{vr.randint(0, 20)}",
        # Seeded by commit so that rebuilding the same commit reproduces the same failure details.
        "frng": lambda tag: random.Random(sha + tag),
        "durations": {  # base seconds, scaled by speed in Writer
            "build": random.randint(60, 150),
            "unit": random.randint(90, 200),
            "qa": random.randint(40, 90),
            "e2e": random.randint(150, 300),
            "prod": random.randint(30, 70),
        },
    }
    print(f"[{now_ts()}] START  {job} #{build_no} branch={branch} commit={sha[:7]}"
          f"{' planned-failure=' + fail if fail else ''} -> {path}", flush=True)

    failure = None
    with open(path, "a", encoding="utf-8", buffering=1) as fh:
        w = Writer(fh, speed)
        w.line(f"Started by {ctx['started_by']}")
        w.line(f"Obtained Jenkinsfile from git https://git.corp.example.com/{cfg['repo']}.git")
        w.line("Running in Durability level: MAX_SURVIVABILITY")
        w.line("[Pipeline] Start of Pipeline")
        w.line("[Pipeline] node")
        w.line(f"Running on build-agent-{random.randint(1, 5):02d} in /var/lib/jenkins/workspace/{job}")

        for name, fn in STAGES:
            if name == "Deploy Prod" and branch != "main":
                stage_skipped(w, name, "when conditional")
                continue
            if failure:
                stage_skipped(w, name, "earlier failure(s)")
                continue
            try:
                fn(w, cfg, ctx)
            except PipelineFailure as e:
                failure = e

        w.line("[Pipeline] stage")
        w.line("[Pipeline] { (Declarative: Post Actions)")
        w.line("[Pipeline] echo")
        w.line(f"Pipeline failed in stage: {failure.stage}" if failure else "Pipeline completed successfully")
        stage_close(w)
        w.line("[Pipeline] }")
        w.line("[Pipeline] // node")
        w.line("[Pipeline] End of Pipeline")
        w.line("Finished: FAILURE" if failure else "Finished: SUCCESS")

    status = "FAILURE" if failure else "SUCCESS"
    print(f"[{now_ts()}] FINISH {job} #{build_no} {status}"
          f"{' (' + failure.stage + ' / ' + failure.kind + ')' if failure else ''}", flush=True)
    return {"job": job, "build": build_no, "status": status, "commit": sha[:7], "branch": branch,
            "author": author, "failed_stage": failure.stage if failure else None,
            "failure": failure.kind if failure else None}


# --------------------------------------------------------------------------
# Scenario + background drivers
# --------------------------------------------------------------------------


def write_manifest(base, record):
    """Record scenario builds outside jobs/ so file-tailing agents do not pick it up."""
    d = os.path.join(base, "_scenarios")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "manifest.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run_scenario(base, name, speed, gap):
    sc = SCENARIOS[name]
    job = sc["job"]
    cfg = JOBS[job]
    shas = {k: uuid.uuid4().hex + uuid.uuid4().hex[:8] for k in sc["commits"]}
    print(f"\n=== SCENARIO {name}: {sc['title']} ===", flush=True)
    results = []
    for i, step in enumerate(sc["steps"]):
        ov = {"branch": sc["branch"], "author": sc["author"], "sha": shas[step["commit"]],
              "msg": sc["commits"][step["commit"]], "started_by": step.get("started_by"), "cve": sc.get("cve")}
        res = run_pipeline(base, job, cfg, speed, fail=step["fail"], overrides=ov)
        res.update({"scenario": name, "step": i + 1, "time": now_ts()})
        write_manifest(base, res)
        results.append(res)
        if i < len(sc["steps"]) - 1:
            time.sleep(gap)
    return results


def job_loop(base, job, cfg, speed, interval, max_runs, failure_rate, stop):
    runs = 0
    time.sleep(random.uniform(0, 5))  # stagger job start times
    while not stop.is_set() and (max_runs == 0 or runs < max_runs):
        fail = pick_failure() if random.random() < failure_rate else None
        run_pipeline(base, job, cfg, speed, fail=fail)
        runs += 1
        if max_runs and runs >= max_runs:
            break
        stop.wait(interval * random.uniform(0.5, 1.5))


def main():
    ap = argparse.ArgumentParser(description="Live Jenkins-style pipeline log generator with failure scenarios",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--base-dir", default="/data/pipeline-logs", help="root directory for jobs/<job>/builds/<n>/log")
    ap.add_argument("--jobs", nargs="*", default=list(JOBS), choices=list(JOBS), help="jobs to simulate (default: all)")
    ap.add_argument("--speed", type=float, default=1.0, help="time compression factor, 1 = real time")
    ap.add_argument("--interval", type=int, default=120, help="average seconds between runs of the same job")
    ap.add_argument("--max-runs", type=int, default=0, help="runs per job in background mode, 0 = forever")
    ap.add_argument("--failure-rate", type=float, default=0.12, help="fraction of background runs that fail (0 to 1)")
    ap.add_argument("--scenario", nargs="+", metavar="NAME", help="play scripted scenario(s) and exit; 'all' runs every scenario")
    ap.add_argument("--scenario-gap", type=int, default=20, help="real seconds between builds inside a scenario")
    ap.add_argument("--list-scenarios", action="store_true", help="list available scenarios and exit")
    a = ap.parse_args()

    if a.list_scenarios:
        for k, v in SCENARIOS.items():
            outcome = " -> ".join((s["fail"] or "SUCCESS") for s in v["steps"])
            print(f"{k:16} {v['job']:22} {v['title']}\n{'':16} {outcome}")
        return

    if a.scenario:
        names = list(SCENARIOS) if "all" in a.scenario else a.scenario
        bad = [n for n in names if n not in SCENARIOS]
        if bad:
            sys.exit(f"Unknown scenario(s): {', '.join(bad)}. Use --list-scenarios.")
        for n in names:
            run_scenario(a.base_dir, n, a.speed, a.scenario_gap)
        return

    stop = threading.Event()
    threads = [
        threading.Thread(target=job_loop, args=(a.base_dir, j, JOBS[j], a.speed, a.interval,
                                                a.max_runs, a.failure_rate, stop), daemon=True)
        for j in a.jobs
    ]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()
        sys.exit(0)


if __name__ == "__main__":
    main()
