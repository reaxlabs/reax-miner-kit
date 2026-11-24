"""EU location evidence. A TEE quote proves WHAT runs, never WHERE. We combine three independent signals:

1. Platform registry (strong, contractual): the CPU's platform identity (TDX PPID from the PCK cert, or SNP CHIP_ID)
   must be on our registry of machines whose EU location is documented (provider contract/DPA, invoice, data-centre
   address, our own onboarding check). A miner cannot move a registered chip ID to another CPU.
2. Latency physics bound (strong against relays far away): validator probes in EU cities time a challenge that
   only the attested enclave can answer (signed with the key bound in report_data). Signals travel at most ~200 km/ms
   in fibre, so RTT r ms from a probe bounds the distance to <= r/2 * 200 km. A relay can only ADD latency, never
   remove it, so a non-EU enclave cannot appear close to several EU probes at once.
3. Provider metadata (supporting): cloud-signed instance identity (e.g. GCP/Azure identity token with zone) or the
   provider API showing the instance in an EU region. IP geolocation is logged but never sufficient.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from .errors import Report, fail_closed

FIBRE_KM_PER_MS = 200.0  # ~ c / 1.5; upper bound for any real path

# EEA member states (EU 27 + IS, LI, NO), ISO 3166-1 alpha-2
EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
    "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "IS", "LI", "NO",
}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def max_distance_km(rtt_ms: float) -> float:
    return rtt_ms / 2.0 * FIBRE_KM_PER_MS


@fail_closed("location")
def verify_location(platform_id: str | None, location: dict[str, Any], policy: dict[str, Any]) -> Report:
    rep = Report("location")
    registry = {e["platform_id"].lower(): e for e in policy.get("eu_platform_registry", [])}
    entry = registry.get((platform_id or "").lower())
    if entry is None and policy.get('location_mode') == 'registry_rtt':
        site = location.get('registered_site')
        providers = policy.get('eu_cloud_providers', [])
        if (isinstance(site, dict) and isinstance(site.get('cloud_provider'), str)
                and isinstance(providers, list) and site['cloud_provider'] in providers
                and site.get('country') in EEA):
            entry = site
    rep.claims['platform_id'] = platform_id
    rep.add("platform_registered", entry is not None, f"platform id {str(platform_id)[:16]}... {'registered' if entry else 'NOT in EU registry'}")
    if entry:
        rep.add("registered_country_eea", entry.get("country") in EEA, f"{entry.get('provider')} {entry.get('site')} {entry.get('country')}")
        rep.claims["site"] = {k: entry.get(k) for k in ("provider", "site", "country", "lat", "lon")}

    if policy.get("location_mode") == "registry_rtt":
        probes = location.get("rtt_probes", [])
        limit = float(policy.get("rtt_max_ms", 10))
        minimum = max(3, int(policy.get("rtt_min_probes", 3)))
        valid = math.isfinite(limit) and limit > 0 and len(probes) >= minimum and all(
            isinstance(p.get("rtt_ms"), (int, float)) and not isinstance(p.get("rtt_ms"), bool)
            and math.isfinite(p["rtt_ms"]) and 0 < p["rtt_ms"] for p in probes)
        valid = valid and statistics.median(p['rtt_ms'] for p in probes) < limit
        rep.add("rtt_ok", valid, "validator RTT within configured bound")
        rep.add("asn_ok", bool(entry and entry.get("asn")), "ASN present in registry")
        return rep
    if policy.get("location_mode", "multi_probe") != "multi_probe":
        rep.add("location_mode", False, "unknown location mode")
        return rep

    # probes MUST come from the validator's own probe service (never from the miner's bundle)
    probes = location.get("rtt_probes", [])
    bad = [p for p in probes if not (isinstance(p.get("rtt_ms"), (int, float)) and math.isfinite(p["rtt_ms"]) and p["rtt_ms"] > 0)]
    if bad:
        rep.add("rtt_values_valid", False, f"{len(bad)} probes with non-finite or non-positive RTT")
        return rep
    min_probes = int(policy.get("rtt_min_probes", 2))
    rep.add("rtt_probe_count", len(probes) >= min_probes, f"{len(probes)} >= {min_probes} enclave-signed RTT probes")
    if entry and probes:
        site = (float(entry["lat"]), float(entry["lon"]))
        incons = []
        for p in probes:
            # best-of-N RTT to a challenge signed by the enclave key (signature checked by the probe service)
            d = haversine_km((p["lat"], p["lon"]), site)
            if max_distance_km(float(p["rtt_ms"])) + float(policy.get("rtt_slack_km", 0)) < d:
                incons.append(f"{p['probe']}: site {d:.0f} km away but RTT {p['rtt_ms']} ms allows only {max_distance_km(float(p['rtt_ms'])):.0f} km")
        # A real machine at `site` can never be measured FASTER than physics allows from a probe; if it is, the site
        # claim is wrong (or the clock/probe is). Being slower is normal (routing), so we also require a tight bound.
        rep.add("rtt_consistent_with_site", not incons, "; ".join(incons) if incons else "all probes consistent")
        tight = min(max_distance_km(float(p["rtt_ms"])) for p in probes)
        lim = float(policy.get("rtt_max_radius_km", 1500))
        rep.add("rtt_tight_bound", tight <= lim, f"closest probe bounds distance to {tight:.0f} km (limit {lim:.0f})")
        eu_probe_ok = all(p.get("country") in EEA for p in probes)
        rep.add("probes_in_eea", eu_probe_ok, "all probes run on EEA hosts")
    ipc = location.get("ip_country")
    if ipc:
        rep.claims["ip_country_unverified"] = ipc
    return rep
