"""oom_kill: memory grows until containers hit their limit and are OOMKilled, repeatedly.

Flavors:
- leak_after_release: a release introduced a leak; memory climbs from the deploy onwards
  in a sawtooth (grow, OOMKilled, restart, grow again).
- unbounded_cache: no recent deploy; an in-process cache without eviction grows with traffic
  since key cardinality jumped a few hours ago. Its stats lines show evictions=0.
"""

from __future__ import annotations

from dataclasses import dataclass

from triage.simulator import alerts, background, catalog, herrings
from triage.simulator.builder import CaseBuilder
from triage.simulator.clock import HOUR, MINUTE, relative
from triage.simulator.rng import Rng
from triage.simulator.scenarios.base import ScenarioResult, common_tags, degrade_observability
from triage.simulator.scenarios.common import caller_error_line
from triage.simulator.schemas import Action, Expected, ToolName
from triage.simulator.spec import CaseSpec, RedHerring

# What the leaking release changed, and the structure that leaks.
LEAKS = {
    "payments-service": (
        "Cache merchant risk profiles in memory to cut scoring latency",
        "merchant risk profiles cached in memory without eviction",
    ),
    "orders-service": (
        "Keep recent customer orders in a process-local cache",
        "customer orders cached in a process-local dict without eviction",
    ),
    "ledger-service": (
        "Add in-memory balance snapshot cache",
        "balance snapshots cached in memory without eviction",
    ),
    "api-gateway": (
        "Buffer full request bodies for audit logging",
        "request bodies buffered for audit logging are never released",
    ),
}
CACHE_NAMES = {
    "payments-service": "merchant_profiles",
    "orders-service": "customer_orders",
    "ledger-service": "balance_snapshots",
}
JAVA_LEAK_FRAME = "com.acme.payments.risk.MerchantProfileCache.put(MerchantProfileCache.java:88)"
RESTART_SPAN_S = {"java": 240, "python": 150, "go": 180}  # time until all pods serve again
ERROR_SPIKE = {"java": (25.0, 45.0), "python": (8.0, 18.0), "go": (6.0, 14.0)}

VARIANTS = (
    CaseSpec("oom_kill", 1, "easy", "dev", "payments-service", "HighErrorRate", 14,
             flavor="leak_after_release", release_hours_ago=3.0),
    CaseSpec("oom_kill", 2, "easy", "test", "orders-service", "PodRestartsHigh", 11,
             severity="warning", flavor="leak_after_release", release_hours_ago=4.5),
    CaseSpec("oom_kill", 3, "easy", "test", "ledger-service", "HighErrorRate", 16,
             flavor="leak_after_release", release_hours_ago=2.5),
    CaseSpec("oom_kill", 4, "easy", "test", "payments-service", "PodRestartsHigh", 20,
             severity="warning", noise="medium", flavor="unbounded_cache"),
    CaseSpec("oom_kill", 5, "medium", "dev", "orders-service", "HighErrorRate", 13,
             noise="medium", flavor="leak_after_release", release_hours_ago=5.0,
             red_herrings=(RedHerring("recent_deploy", "api-gateway"),)),
    CaseSpec("oom_kill", 6, "medium", "test", "ledger-service", "HighErrorRate", 19,
             severity="warning", noise="medium", flavor="unbounded_cache",
             red_herrings=(RedHerring("redis_latency_spike"),)),
    CaseSpec("oom_kill", 7, "medium", "test", "payments-service", "HighLatencyP95", 10,
             severity="warning", noise="medium", flavor="leak_after_release",
             release_hours_ago=6.0, metric_gaps=True,
             red_herrings=(RedHerring("provider_429_burst"),)),
    CaseSpec("oom_kill", 8, "medium", "test", "api-gateway", "HighErrorRate", 18,
             noise="high", flavor="leak_after_release", release_hours_ago=3.5,
             red_herrings=(RedHerring("recent_deploy", "orders-service"),)),
    CaseSpec("oom_kill", 9, "hard", "dev", "orders-service", "HighErrorRate", 12,
             noise="high", flavor="leak_after_release", release_hours_ago=20.0,
             metric_gaps=True, red_herrings=(RedHerring("same_service_config"),)),
    CaseSpec("oom_kill", 10, "hard", "test", "payments-service", "HighErrorRate", 15,
             alert_service="api-gateway", noise="high", flavor="leak_after_release",
             release_hours_ago=4.0, missing_metrics=("memory",),
             red_herrings=(RedHerring("provider_429_burst"),
                           RedHerring("recent_deploy", "ledger-service"))),
)  # fmt: skip


@dataclass(frozen=True)
class KillCluster:
    """All pods of the service get OOMKilled within ~2 minutes of each other."""

    pod_kills: tuple[tuple[str, int], ...]  # (pod, ts)

    @property
    def first(self) -> int:
        return self.pod_kills[0][1]

    @property
    def last(self) -> int:
        return self.pod_kills[-1][1]


def build(b: CaseBuilder, spec: CaseSpec) -> ScenarioResult:
    service = spec.service
    runtime = b.topology.services[service].runtime
    limit = float(b.topology.services[service].memory_limit_mib or 1024)
    rng = b.rng.derive("oom")
    leak = spec.flavor == "leak_after_release"

    # 1. Plan deploys, then let the background fill a normal day.
    if spec.alert == "HighLatencyP95":  # GC thrash fires the alert before the next kill
        anchor = b.alert_ts + rng.randint(60, 200)
    else:  # the alert reacts to the kill cluster that just happened
        anchor = b.alert_ts - rng.randint(90, 170)
    release_handle = None
    if leak:
        release_ts = b.alert_ts - int(spec.release_hours_ago * HOUR) - rng.randint(0, 40) * MINUTE
        release_handle = b.plan_deploy(release_ts, service, "code", LEAKS[service][0])
        b.keep_quiet(service, release_ts - HOUR, b.now_ts)
        growth_start = release_ts
    else:
        growth_start = b.alert_ts - rng.randint(4, 7) * HOUR
        b.keep_quiet(service, b.alert_ts - 5 * 24 * HOUR, b.now_ts)
    planned = [herrings.plan(b, h, service) for h in spec.red_herrings]
    background.finalize(b)

    # 2. Memory model and kill schedule.
    base = limit * background.MEMORY_FRACTION[runtime] * rng.uniform(0.95, 1.05)
    peak = limit * rng.uniform(0.975, 0.99)
    # A restart-count alert fires at the first kill cluster; with earlier kills it would have
    # been firing for hours already. So those variants model the very first OOM.
    first_oom = spec.alert == "PodRestartsHigh"
    if leak:
        memory, resets = _leak_memory(b, rng, growth_start, anchor, base, peak, first_oom)
    else:
        memory, resets, growth_start = _cache_memory(
            b, rng, growth_start, anchor, base, peak, first_oom
        )
    clusters = tuple(_cluster(b, rng, service, ts) for ts in resets)

    # 3. Overlay signals.
    _apply_memory(b, service, memory, limit, runtime, rng)
    for cluster in clusters:
        _restart_effects(b, service, runtime, cluster, rng)
        _kill_logs(b, service, runtime, cluster, rng, limit)
    restart_count = _restart_counts(clusters, growth_start)
    _oom_event_logs(b, service, clusters, restart_count, rng)
    if not leak:
        _cache_stats_logs(b, service, runtime, memory, base, rng)
    herring_notes = tuple(herrings.apply(b, p) for p in planned)
    alerts.enforce_condition(b, spec.alert, spec.alerting_service)
    degrade_observability(b, spec, tuple(c.first for c in clusters))

    # 4. Alert and answer key.
    recent_kills = sum(
        1 for c in clusters for _, ts in c.pod_kills if b.alert_ts - HOUR <= ts <= b.alert_ts
    )
    alert = alerts.build_alert(
        b, spec.alert, spec.alerting_service, spec.severity, restarts=recent_kills
    )
    must_call: tuple[ToolName, ...]
    actions: tuple[Action, ...]
    if leak and release_handle is not None:
        release = b.deploy(release_handle)
        root_cause = (
            f"{service} {release.version} (deployed {relative(release.ts, b.alert_ts)}: "
            f"'{release.description}') introduced a memory leak ({LEAKS[service][1]}); "
            f"containers reach the {limit:.0f} MiB limit and are OOMKilled repeatedly"
        )
        keywords: tuple[str, ...] = ("memory", release.version)
        actions = (
            Action(type="rollback", target=service, to_version=release.previous_version),
            Action(type="restart", target=service),
        )
        must_call = ("get_deploys", "search_logs")
    else:
        root_cause = (
            f"unbounded in-process cache '{CACHE_NAMES[service]}' in {service} (evictions=0) "
            f"grows with traffic until containers reach the {limit:.0f} MiB limit and are "
            "OOMKilled; there was no recent deploy"
        )
        keywords = ("memory", "cache")
        actions = (Action(type="restart", target=service),)
        must_call = ("search_logs",)
    if "memory" not in spec.missing_metrics:
        must_call = (*must_call, "get_metrics")
    tags = common_tags(spec) + (("old_release",) if spec.release_hours_ago >= 12 else ())
    tags += () if leak else ("no_recent_deploy",)
    expected = Expected(
        root_cause=root_cause,
        root_cause_keywords=keywords,
        root_service=service,
        should_escalate=False,
        must_call_tools=must_call,
        acceptable_actions=actions,
        red_herrings=herring_notes,
    )
    return ScenarioResult(alert, expected, tags)


# --- memory models -----------------------------------------------------------------------


def _leak_memory(
    b: CaseBuilder,
    rng: Rng,
    release: int,
    anchor: int,
    base: float,
    peak: float,
    first_oom: bool,
) -> tuple[dict[int, float], tuple[int, ...]]:
    """Constant-rate leak from the release; kills evenly spaced so one lands on ``anchor``."""
    target = rng.uniform(25, 45) if anchor - release <= 8 * HOUR else rng.uniform(80, 110)
    cycles = 1 if first_oom else max(1, round((anchor - release) / (target * MINUTE)))
    period = (anchor - release) / cycles
    resets = []
    k = 1
    while release + round(k * period) <= b.now_ts:
        resets.append(release + round(k * period))
        k += 1
    memory = {}
    for ts in b.times:
        if ts < release:
            continue
        last = max([release, *(r for r in resets if r <= ts)])
        memory[ts] = base + (peak - base) * (ts - last) / period
    return memory, tuple(resets)


def _cache_memory(
    b: CaseBuilder,
    rng: Rng,
    growth_start: int,
    anchor: int,
    base: float,
    peak: float,
    first_oom: bool,
) -> tuple[dict[int, float], tuple[int, ...], int]:
    """Growth proportional to traffic since ``growth_start``; kills found by integrating
    backwards from ``anchor`` so that one kill cluster lands right before the alert."""
    cycle_min = rng.uniform(50, 80)
    rate = (peak - base) / cycle_min / background.traffic(anchor)  # MiB per minute at traffic 1
    resets = [anchor]
    acc, t = 0.0, anchor
    if first_oom:  # the cache grew from (almost) empty to the limit exactly once
        while acc < 0.97 * (peak - base):
            t -= MINUTE
            acc += rate * background.traffic(t)
    else:
        while t - MINUTE > growth_start:
            t -= MINUTE
            acc += rate * background.traffic(t)
            if acc >= peak - base:
                resets.append(t)
                acc = 0.0
        while acc < 0.6 * (peak - base):  # start growth early enough that the cache begins small
            t -= MINUTE
            acc += rate * background.traffic(t)
    growth_start = t
    level_at_start = peak - acc  # memory when growth began: cache already partly filled
    acc, t = 0.0, anchor
    while t < b.now_ts:  # kills after the anchor, if any fit before "now"
        t += MINUTE
        acc += rate * background.traffic(t)
        if acc >= peak - base:
            resets.append(t)
            acc = 0.0
    resets.sort()
    memory = {}
    for ts in b.times:
        if ts < growth_start:
            memory[ts] = level_at_start
            continue
        previous = [r for r in resets if r <= ts]
        start, level = (previous[-1], base) if previous else (growth_start, level_at_start)
        for minute in range(start, ts, MINUTE):
            level += rate * background.traffic(minute)
        memory[ts] = min(level, peak)
    return memory, tuple(resets), growth_start


def _cluster(b: CaseBuilder, rng: Rng, service: str, ts: int) -> KillCluster:
    """Pods are killed in the order they fill up; ``ts`` is when the last one dies."""
    pods = b.pods_at(service, ts)
    offsets = sorted(rng.randint(10, 130) for _ in pods[1:])
    kills = [(pods[0], ts - (offsets[-1] if offsets else 0))]
    kills += [(pod, kills[0][1] + offset) for pod, offset in zip(pods[1:], offsets, strict=True)]
    return KillCluster(tuple(kills))


# --- overlays ------------------------------------------------------------------------------


def _apply_memory(
    b: CaseBuilder, service: str, memory: dict[int, float], limit: float, runtime: str, rng: Rng
) -> None:
    values = b.values(service, "memory")
    for i, ts in enumerate(b.times):
        if ts in memory:
            values[i] = memory[ts] + limit * rng.uniform(-0.004, 0.004)
            if runtime == "java":  # GC thrash as the heap fills: latency and CPU climb
                pressure = max(0.0, (memory[ts] / limit - 0.8) / 0.19)
                b.scale(service, "latency_p95", ts, ts + 1, 1 + 9 * pressure)
                b.add(service, "cpu", ts, ts + 1, 30 * pressure)


def _restart_effects(
    b: CaseBuilder, service: str, runtime: str, cluster: KillCluster, rng: Rng
) -> None:
    start, end = cluster.first, cluster.last + RESTART_SPAN_S[runtime]
    low, high = ERROR_SPIKE[runtime]
    spike = rng.uniform(low, high)
    b.floor(service, "error_rate", start - MINUTE, end, spike * 0.6)  # degrading before the kill
    b.floor(service, "error_rate", start, end, spike)
    b.scale(service, "latency_p95", start, end, rng.uniform(2.0, 3.5))
    b.add(service, "cpu", cluster.last, end, 40 if runtime == "java" else 10)  # JIT/warm-up
    b.scale(service, "rps", start, end, 0.9)
    for caller in b.topology.dependents_of(service):
        share = rng.uniform(0.15, 0.3)  # fraction of the caller's requests that need this service
        b.add(caller, "error_rate", start, end, spike * share)


def _kill_logs(
    b: CaseBuilder, service: str, runtime: str, cluster: KillCluster, rng: Rng, limit: float
) -> None:
    for pod, ts in cluster.pod_kills:
        t = ts * 1000
        if runtime == "java":
            heap = int(limit * 0.85)
            for _ in range(rng.randint(2, 4)):
                used = int(heap * rng.uniform(0.95, 0.995))
                msg = (
                    "c.a.p.jvm.GcMonitor : long GC pause: G1 Evacuation Pause "
                    f"{rng.randint(900, 2600)}ms, heap {used}M->{used - rng.randint(2, 30)}M "
                    f"({heap}M)"
                )
                b.log(t - rng.randint(30, 480) * 1000, service, "WARN", msg, pod=pod)
            for n in range(rng.randint(1, 2)):
                msg = (
                    "o.a.c.c.C.[.[.[/].[dispatcherServlet] : Servlet.service() for servlet "
                    "[dispatcherServlet] threw exception [Handler dispatch failed: "
                    "java.lang.OutOfMemoryError: Java heap space]"
                )
                if n == 0:
                    msg += (
                        "\njava.lang.OutOfMemoryError: Java heap space"
                        "\n\tat java.base/java.util.HashMap.resize(HashMap.java:710)"
                        f"\n\tat {JAVA_LEAK_FRAME}"
                        "\n\tat com.acme.payments.risk.RiskScorer.score(RiskScorer.java:57)"
                    )
                b.log(t - rng.randint(5, 90) * 1000, service, "ERROR", msg, pod=pod)
        elif runtime == "python":
            for _ in range(rng.randint(1, 2)):
                msg = (
                    f"gunicorn.error: Worker (pid:{rng.randint(20, 400)}) was sent SIGKILL! "
                    "Perhaps out of memory?"
                )
                b.log(t - rng.randint(5, 40) * 1000, service, "ERROR", msg, pod=pod)
        version = b.app_version_at(service, ts)
        t += rng.randint(8000, 20000) + (20000 if runtime == "java" else 0)
        for level, msg in catalog.startup_lines(service, version, rng):
            b.log(t, service, level, msg, pod=pod)
            t += rng.randint(200, 3000)
        for caller in b.topology.dependents_of(service):
            for _ in range(rng.randint(1, 3)):
                ts_ms = ts * 1000 + rng.randint(0, 60000)
                level, msg = caller_error_line(caller, service, "refused", rng)
                b.log(ts_ms, caller, level, msg)


def _restart_counts(clusters: tuple[KillCluster, ...], since: int) -> dict[tuple[str, int], int]:
    """restartCount of each pod at each of its kills, counting kills since ``since``."""
    counts: dict[str, int] = {}
    result = {}
    for cluster in clusters:
        for pod, ts in cluster.pod_kills:
            if ts >= since:
                counts[pod] = counts.get(pod, 0) + 1
            result[(pod, ts)] = counts.get(pod, 0) or 1
    return result


def _oom_event_logs(
    b: CaseBuilder,
    service: str,
    clusters: tuple[KillCluster, ...],
    restart_count: dict[tuple[str, int], int],
    rng: Rng,
) -> None:
    previous_kill: dict[str, int] = {}
    for cluster in clusters:
        for pod, ts in cluster.pod_kills:
            # kubelet resets the crash back-off after 10 minutes of healthy running, so the
            # Back-off event only appears when the same container crashes again quickly.
            crashed_recently = ts - previous_kill.get(pod, -(10**12)) < 10 * MINUTE
            previous_kill[pod] = ts
            count = restart_count[(pod, ts)]
            t = ts * 1000
            b.log(
                t, service, "WARN",
                f"Container {service} in pod {pod} was OOMKilled (exit code 137); "
                f"restartCount={count}",
                source="k8s", pod=pod,
            )  # fmt: skip
            ip = b.pod_ip(pod)
            b.log(
                t + rng.randint(2000, 6000), service, "WARN",
                f'Readiness probe failed: Get "http://{ip}:8080/healthz": dial tcp {ip}:8080: '
                "connect: connection refused",
                source="k8s", pod=pod,
            )  # fmt: skip
            if crashed_recently and count >= 3:
                b.log(
                    t + rng.randint(7000, 12000), service, "WARN",
                    f"Back-off restarting failed container {service} in pod {pod}",
                    source="k8s", pod=pod,
                )  # fmt: skip


def _cache_stats_logs(
    b: CaseBuilder, service: str, runtime: str, memory: dict[int, float], base: float, rng: Rng
) -> None:
    """Periodic cache statistics: entry count tracks memory, and nothing is ever evicted."""
    name = CACHE_NAMES[service]
    for ts in b.times[::5]:
        entries = int(max(memory.get(ts, base) - base, 1.0) * rng.uniform(850, 950))
        hit_ratio = rng.uniform(0.88, 0.95)
        for pod in b.pods_at(service, ts):
            if runtime == "java":
                msg = (
                    f"c.a.p.risk.MerchantProfileCache : cache stats name={name} size={entries} "
                    f"hitRate={hit_ratio:.2f} evictions=0"
                )
            elif runtime == "python":
                msg = (
                    f"orders.cache: local cache stats name={name} entries={entries} "
                    f"hit_ratio={hit_ratio:.2f} evictions=0"
                )
            else:
                msg = f'msg="cache stats" name={name} entries={entries} evictions=0'
            b.log(ts * 1000 + rng.randint(0, 20000), service, "INFO", msg, pod=pod)
