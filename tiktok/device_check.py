"""Pre-flight self-test for an antidetect profile: catch a WebRTC / timezone / geo leak
BEFORE TikTok does and bans the account.

Runs inside the already-open page (so it goes through the SAME proxy + device the session
uses) and cross-checks three things that must agree with the proxy's exit country:

  * public IP    — what the site actually sees (fetched through the browser → proxy)
  * WebRTC       — must NOT expose a public IP different from that exit IP (a classic leak)
  * timezone     — Intl tz must map to the proxy's country, not the host machine's

Used by warmup: if anything disagrees, the profile is mis-built — back off rather than warm
a profile that will get flagged. Best-effort: a failed probe never raises, it just reports
no problem (we don't want a flaky network call to abort a healthy profile)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Country → set of acceptable IANA timezone *prefixes*. Coarse on purpose: we only want to
# catch a gross mismatch (host tz Europe/Kyiv leaking under a US proxy), not police DST.
_TZ_COUNTRY = {
    "US": ("America/",), "CA": ("America/",), "GB": ("Europe/London",),
    "UA": ("Europe/Kyiv", "Europe/Kiev", "Europe/Simferopol"),
    "DE": ("Europe/Berlin", "Europe/Busingen"), "FR": ("Europe/Paris",),
    "NL": ("Europe/Amsterdam",), "PL": ("Europe/Warsaw",), "AU": ("Australia/",),
}

_JS_PROBE = """
async () => {
  const out = {tz: null, ip: null, ipCountry: null, webrtc: []};
  try { out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) {}
  try {
    const r = await fetch('http://ip-api.com/json/?fields=status,countryCode,query',
                          {cache: 'no-store'});
    const j = await r.json();
    if (j.status === 'success') { out.ip = j.query; out.ipCountry = j.countryCode; }
  } catch (e) {}
  try {
    const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
    pc.createDataChannel('x');
    await pc.createOffer().then(o => pc.setLocalDescription(o));
    await new Promise(res => {
      let done = false;
      const finish = () => { if (!done) { done = true; res(); } };
      pc.onicecandidate = e => {
        if (!e.candidate) return finish();
        const m = (e.candidate.candidate || '').match(/(\\d{1,3}(?:\\.\\d{1,3}){3})/);
        if (m) out.webrtc.push(m[1]);
      };
      setTimeout(finish, 2500);
    });
    pc.close();
  } catch (e) {}
  return out;
}
"""


def _is_private(ip: str) -> bool:
    return (ip.startswith("10.") or ip.startswith("192.168.")
            or ip.startswith("127.") or ip.startswith("169.254.")
            or any(ip.startswith(f"172.{n}.") for n in range(16, 32)))


async def self_test(page, expected_country: str | None) -> tuple[bool, list[str]]:
    """Return (ok, problems). ok=False means the profile leaks and should NOT be warmed."""
    try:
        data = await page.evaluate(_JS_PROBE)
    except Exception as e:
        logger.warning("device self-test probe failed (skipping): %s", e)
        return True, []

    problems: list[str] = []
    exit_ip = data.get("ip")
    ip_country = (data.get("ipCountry") or "").upper() or None
    tz = data.get("tz")

    # WebRTC: any PUBLIC candidate that isn't the proxy exit IP = the real host IP leaking.
    for cand in data.get("webrtc") or []:
        if not _is_private(cand) and cand != exit_ip:
            problems.append(f"WebRTC leaks public IP {cand} (exit {exit_ip})")
            break

    # geo: site-visible country must match the proxy we think we're on.
    if expected_country and ip_country and ip_country != expected_country.upper():
        problems.append(f"exit country {ip_country} != expected {expected_country.upper()}")

    # timezone: must map to the exit country (host-tz leaking under a foreign proxy).
    cc = ip_country or (expected_country.upper() if expected_country else None)
    if tz and cc and cc in _TZ_COUNTRY:
        if not any(tz.startswith(p) for p in _TZ_COUNTRY[cc]):
            problems.append(f"timezone {tz} doesn't match country {cc}")

    return (not problems), problems
