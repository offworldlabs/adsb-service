"""Pure SBS/BaseStation line validation (DESIGN.md §4; spec §12.1/§12.2/§12.4).

Pre-filters upstream lines to readsb's accepted subset (vendor/readsb
net_io.c decodeSbsLine: only ``MSG``, length 20-199, >=22 comma fields,
6-hex ICAO with optional ``~``) plus stricter range and timestamp guards,
so misleading records are counted here and never reach readsb.

ADSBHub emits the ICAO field WITHOUT leading-zero padding (``D0EEC`` for
0D0EEC), which readsb rejects outright — so the ICAO field is accepted at
1-6 hex chars and left-zero-padded to 6 before relay. That padding is the
single, enumerated exception to the DESIGN.md §4 verbatim-relay rule
(ADR-0007); every other byte of a relayed line is forwarded unchanged.

Contract: bytes in, a ValidationResult out — no exception ever escapes for
arbitrary input (spec §12.1: never terminate the stream on one bad record;
§21.3 fuzz gate). ``relay_line`` carries the exact bytes to forward
(without terminator) whenever ``relay`` is True, and re-validating them is
a fixed point: same result, ``normalized`` False, bytes unchanged.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

OK = "ok"
INVALID_STRUCTURE = "invalid_structure"
INVALID_RANGE = "invalid_range"
STALE = "stale"
FUTURE = "future"
UNPARSEABLE_TS = "unparseable_ts"

# Exact result label set of retina_adsb_provider_records_total{result=...} (DESIGN.md §4)
RESULT_LABELS = (OK, INVALID_STRUCTURE, INVALID_RANGE, STALE, FUTURE, UNPARSEABLE_TS)
REJECT_REASONS = frozenset({INVALID_STRUCTURE, INVALID_RANGE, STALE, FUTURE})

# retina_adsb_provider_records_normalized_total{normalization=...}: the
# enumerated exceptions to verbatim relay. Exactly one exists (ADR-0007).
ICAO_ZERO_PAD = "icao_zero_pad"
NORMALIZATION_LABELS = (ICAO_ZERO_PAD,)

# readsb's own line acceptance window (net_io.c:3046-3049), enforced pre-relay;
# MAX_LINE_BYTES is re-checked after ICAO padding (ADR-0007) because the
# inserted zeros can push a 199-byte line past readsb's cap
MIN_LINE_BYTES = 20
MAX_LINE_BYTES = 199
MIN_FIELDS = 22

_MSG_TYPES = frozenset("12345678")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_OCTAL_DIGITS = frozenset("01234567")
_CALLSIGN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ")

# sec-5: only numeric forms C strtod consumes in full and parses to the same
# value as Python float() — no '_' separators, inf/nan, hex floats, or
# whitespace; readsb (net_io.c strtod) must never read a relayed field
# differently than the validator did.
_STRTOD_SAFE_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

# sc-1: readsb accepts ANY finite heading (net_io.c:3207-3216: strtod +
# isfinite only) and the verbatim relay cannot rewrite; spec §12.2 asks only
# to reject extreme parser overflow — bound at ten full turns.
TRACK_OVERFLOW_BOUND = 3600.0

# spec §12.2 default altitude window; overridable via adsbhub.altitude_min_ft /
# adsbhub.altitude_max_ft ('configurable', sc-3).
ALTITUDE_MIN_FT = -2000.0
ALTITUDE_MAX_FT = 100000.0


@dataclass(frozen=True, slots=True)
class ValidationResult:
    result: str
    relay: bool
    generated_ts: float | None = None
    delay: float | None = None
    # exact bytes to forward, without CR/LF terminator; None when not relayed
    relay_line: bytes | None = None
    # True iff the ICAO field was left-zero-padded (ADR-0007)
    normalized: bool = False


def validate_line(
    raw: bytes,
    *,
    now: float,
    future_tolerance: float = 1.0,
    max_record_age: float = 60.0,
    altitude_min_ft: float = ALTITUDE_MIN_FT,
    altitude_max_ft: float = ALTITUDE_MAX_FT,
) -> ValidationResult:
    """Validate one SBS line (without or with trailing CR/LF, both accepted).

    ``now`` is the adapter wall clock in Unix seconds. Provider timestamps are
    always interpreted as UTC: pinned readsb hardcodes UTC via ``timegm``
    (net_io.c:3153), so a configurable timezone here would only make the
    validator disagree with the consumer of the verbatim relay.
    """
    try:
        return _validate(bytes(raw), now, future_tolerance, max_record_age,
                         altitude_min_ft, altitude_max_ft)
    except Exception:
        # spec §12.1 / §21.3: arbitrary input must never raise past this boundary
        return ValidationResult(INVALID_STRUCTURE, relay=False)


def _validate(raw: bytes, now: float, future_tolerance: float,
              max_record_age: float,
              altitude_min_ft: float, altitude_max_ft: float) -> ValidationResult:
    line = raw.rstrip(b"\r\n")
    if not MIN_LINE_BYTES <= len(line) <= MAX_LINE_BYTES:
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    if any(b < 0x20 or b > 0x7E for b in line):
        # printable ASCII only: rejects NUL, control chars, and non-ASCII bytes
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    # SBS/BaseStation has no quoting, so a bare comma split is the exact framing
    # readsb itself uses (net_io.c:3096) — spec §12.4 quoting caveat satisfied.
    fields = line.decode("ascii").split(",")
    if len(fields) < MIN_FIELDS:
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    if fields[0] != "MSG":
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    if fields[1] not in _MSG_TYPES:
        # spec §12.4: only explicitly tested variants (MSG 1..8, golden fixtures)
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    icao = fields[4]
    prefix, body = ("~", icao[1:]) if icao.startswith("~") else ("", icao)
    # ADR-0007: 1-6 hex chars, not readsb's 6-7 — ADSBHub drops the leading
    # zeros of addresses below 0x100000 (Mexico, Cuba, much of Africa).
    if not 1 <= len(body) <= 6 or any(c not in _HEX_DIGITS for c in body):
        return ValidationResult(INVALID_STRUCTURE, relay=False)
    normalized = len(body) < 6
    if normalized:
        # the sole exception to verbatim relay (DESIGN.md §4, ADR-0007):
        # left-pad with ASCII '0' so readsb accepts the line and keys the
        # aircraft as the '%06x' spec §9.1 demands. Case is left alone.
        fields[4] = prefix + body.rjust(6, "0")
        line = ",".join(fields).encode("ascii")
        if len(line) > MAX_LINE_BYTES:
            # the inserted zeros pushed the line past readsb's own cap
            # (net_io.c:3046-3049), which would silently drop it — reject
            # here instead of relaying bytes the consumer discards
            return ValidationResult(INVALID_STRUCTURE, relay=False)

    if not _ranges_ok(fields, altitude_min_ft, altitude_max_ft):
        return ValidationResult(INVALID_RANGE, relay=False, normalized=normalized)

    generated = _parse_generated_ts(fields[6], fields[7])
    if generated is None:
        # DESIGN.md §4: relay (readsb stamps arrival = conservative), count separately
        return ValidationResult(UNPARSEABLE_TS, relay=True,
                                relay_line=line, normalized=normalized)
    delay = now - generated
    if generated > now + future_tolerance:
        return ValidationResult(FUTURE, relay=False, generated_ts=generated, delay=delay,
                                normalized=normalized)
    if delay > max_record_age:
        # guards the readsb clamp hazard: >20 min stale gets stamped as fresh (ADR-0004)
        return ValidationResult(STALE, relay=False, generated_ts=generated, delay=delay,
                                normalized=normalized)
    return ValidationResult(OK, relay=True, generated_ts=generated, delay=delay,
                            relay_line=line, normalized=normalized)


def _ranges_ok(fields: list[str], altitude_min_ft: float, altitude_max_ft: float) -> bool:
    # spec §12.2: checks apply only when a field is non-empty; empty = absent,
    # never zero-default.
    callsign = fields[10].rstrip(" ")  # trim right padding, preserve internal chars (§12.1)
    if callsign and (len(callsign) > 16 or any(c not in _CALLSIGN_CHARS for c in callsign)):
        return False
    if not _num_ok(fields[11], altitude_min_ft, altitude_max_ft):  # baro altitude, ft
        return False
    if not _num_ok(fields[12], 0.0, 1500.0):            # ground speed, kt
        return False
    # track: any finite value readsb would accept, bar extreme overflow (sc-1)
    if not _num_ok(fields[13], -TRACK_OVERFLOW_BOUND, TRACK_OVERFLOW_BOUND):
        return False
    if not _num_ok(fields[14], -90.0, 90.0):            # latitude
        return False
    if not _num_ok(fields[15], -180.0, 180.0):          # longitude
        return False
    if not _num_ok(fields[16], -20000.0, 20000.0):      # vertical rate, ft/min
        return False
    squawk = fields[17]
    if squawk and (len(squawk) != 4 or any(c not in _OCTAL_DIGITS for c in squawk)):
        return False
    return True


def _num_ok(field: str, lo: float, hi: float) -> bool:
    if not field:
        return True
    if not _STRTOD_SAFE_RE.fullmatch(field):
        # sec-5: forms float() reads but strtod would not consume identically
        # ('3_6', 'inf', '0x1p3', whitespace) must never be relayed
        return False
    value = float(field)
    if not math.isfinite(value):  # spec §12.1: reject NaN and infinity
        return False
    return lo <= value <= hi


# --- SBS generated-timestamp parsing: exact parity with pinned readsb ---
#
# readsb (net_io.c:3141-3161) computes the timestamp as:
#   1. strsep the time field at the FIRST '.'; the remainder goes through
#      strtol(remainder, NULL, 10) and is added as a LITERAL INTEGER COUNT OF
#      MILLISECONDS ('.32' = 32 ms, '.5' = 5 ms, '.3200' = 3200 ms, '.abc' =
#      0 ms) — NOT as a decimal fraction of a second;
#   2. glibc strptime("%Y/%m/%d %H:%M:%S") over date + ' ' + time-prefix,
#      then timegm (always UTC);
#   3. on strptime failure only, fall back to arrival time.
# The validator must reproduce this exactly, or the ADR-0004 stale/future
# guard operates on a different value than readsb will stamp on the relayed
# bytes (e.g. '%f'-style parsing read '12:00:00.0000000' as unparseable while
# readsb parsed the real — possibly 30-min-stale — timestamp and clamped it
# to arrival, serving stale data as fresh).
#
# glibc strptime semantics reproduced below (verified against glibc): each
# numeric field skips leading whitespace, consumes at most 4 (year) / 2
# digits, STOPS EARLY once another digit would exceed the field maximum
# (e.g. seconds '79' parses as 7 with '9' left over), then range-checks
# (m 1-12, d 1-31, H 0-23, M 0-59, S 0-61 — no calendar cross-check, so
# 02/31 normalizes forward like timegm does); trailing garbage after the
# seconds is ignored. The regex alternations encode exactly that greedy
# consume-then-range-check order; delimiters are never digits, so regex
# backtracking cannot accept anything glibc's non-backtracking parser
# rejects.
_WS = r"[ \t\n\r\f\v]*"
_TS_PREFIX_RE = re.compile(
    _WS + r"(?P<Y>\d{1,4})(?!\d)/"
    + _WS + r"(?P<m>1[0-2]|0[1-9]|[2-9]|1(?!\d))/"
    + _WS + r"(?P<d>[12][0-9]|3[01]|0[1-9]|[4-9]|[1-9](?!\d))"
    + _WS + r"(?P<H>[01][0-9]|2[0-3]|[3-9]|[0-9](?!\d)):"
    + _WS + r"(?P<M>[0-5][0-9]|[6-9]|[0-9](?!\d)):"
    + _WS + r"(?P<S>[0-5][0-9]|6[01]|[7-9]|[0-9](?!\d))"
)
# C strtol prefix: optional whitespace, optional sign, leading digits (0 if
# none). Deliberately NOT emulated: C's int truncation of strtol overflow
# (|milli| >= 2^31, i.e. a 10+-digit fraction). In every such case the two
# computed timestamps differ by a multiple of ~49.7 days, so whenever the
# Python value passes the +/-60 s guard below, readsb either computes the
# same value or clamps to arrival — the divergence can only reject extra
# absurd lines, never relay stale data as fresh.
_STRTOL_PREFIX_RE = re.compile(r"[ \t\n\r\f\v]*[+-]?[0-9]+")

_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
_DAYS_400_YEARS = 146097  # exact Gregorian cycle, for the year-0 case below


def _timegm(year: int, month: int, day: int, hour: int, minute: int, second: int) -> int:
    """C timegm over a strptime-filled struct tm: pure proleptic-Gregorian
    arithmetic, out-of-range days (02/31) and leap seconds (:60/:61) roll
    forward instead of failing."""
    if year == 0:
        # datetime.date starts at year 1; year 0 shares year 400's leap layout
        ordinal = date(400, month, 1).toordinal() - _DAYS_400_YEARS
    else:
        ordinal = date(year, month, 1).toordinal()
    days = ordinal - _EPOCH_ORDINAL + day - 1
    return ((days * 24 + hour) * 60 + minute) * 60 + second


def _parse_generated_ts(date_field: str, time_field: str) -> float | None:
    """Parse SBS fields 7/8 (date/time message generated) to Unix seconds,
    UTC, exactly as pinned readsb will (net_io.c:3141-3161; see above).

    None (-> UNPARSEABLE_TS, relayed) exactly and only when readsb's own
    strptime would fail and stamp arrival time instead.
    """
    time_prefix, dot, fraction = time_field.partition(".")
    milli = 0
    if dot:
        matched = _STRTOL_PREFIX_RE.match(fraction)
        if matched:
            milli = int(matched.group(0))
    prefix = _TS_PREFIX_RE.match(f"{date_field} {time_prefix}")
    if prefix is None:
        return None
    seconds = _timegm(int(prefix["Y"]), int(prefix["m"]), int(prefix["d"]),
                      int(prefix["H"]), int(prefix["M"]), int(prefix["S"]))
    return seconds + milli / 1000.0
