"""Hypothesis property tests over the SBS validator (spec §21.3 release gate,
DESIGN.md §7): arbitrary bytes never raise, non-MSG is never relayed, a
relayed line re-validates as relayable and unchanged (ADR-0007 fixed point),
the relayed bytes differ from the input only by ICAO leading zeros
(ADR-0007, the one enumerated rewrite), relayed numeric fields re-parse
identically under a strtod-simulating parser (sec-5), generated timestamps
match a reference reimplementation of readsb's exact parsing algorithm
(strptime prefix + strtol literal-milliseconds fraction, ADR-0004 parity),
and the read-loop framing survives arbitrary segmented byte streams (sec-4).
Corpus seeded from the fixtures."""

import asyncio
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from adsbhub_adapter.adapter import Adapter
from adsbhub_adapter.config import load_config_dict

from adsbhub_adapter.sbs import (
    FUTURE,
    MAX_LINE_BYTES,
    MIN_LINE_BYTES,
    OK,
    RESULT_LABELS,
    STALE,
    UNPARSEABLE_TS,
    validate_line,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sbs"
NOW = datetime(2026, 8, 29, 12, 0, 5, tzinfo=timezone.utc).timestamp()

GOLDEN = [(FIXTURES / f"golden_msg_{t}.txt").read_bytes().splitlines()[0] for t in range(1, 9)]
CORPUS = [l for l in (FIXTURES / "malformed_corpus.txt").read_bytes().split(b"\n") if l]
SEEDS = GOLDEN + CORPUS

RESULT_SET = frozenset(RESULT_LABELS)


def _seed_examples(func):
    for line in SEEDS:
        func = example(line)(func)
        func = example(line + b"\r\n")(func)
    return func


@given(st.binary(max_size=512))
@_seed_examples
def test_arbitrary_bytes_never_raise(data: bytes):
    res = validate_line(data, now=NOW)
    assert res.result in RESULT_SET
    assert isinstance(res.relay, bool)


@given(st.binary(max_size=512))
@_seed_examples
def test_never_relay_non_msg(data: bytes):
    res = validate_line(data, now=NOW)
    if res.relay:
        # the forwarded bytes, not the raw input, are what readsb must accept
        relayed = res.relay_line
        assert relayed is not None
        assert relayed.split(b",")[0] == b"MSG"
        assert MIN_LINE_BYTES <= len(relayed) <= MAX_LINE_BYTES
        assert len(relayed.split(b",")) >= 22


@given(st.binary(max_size=512))
@_seed_examples
def test_relayed_line_revalidates(data: bytes):
    """The relayed bytes are a fixed point of the validator (ADR-0007): one
    padding pass, then nothing changes — otherwise the relay could rewrite a
    line the consumer would judge differently than the validator did."""
    res = validate_line(data, now=NOW)
    if res.relay:
        assert res.relay_line is not None
        again = validate_line(res.relay_line, now=NOW)
        assert again.result == res.result
        assert again.relay is True
        assert again.result in (OK, UNPARSEABLE_TS)
        assert again.relay_line == res.relay_line
        assert again.normalized is False
        assert again.generated_ts == res.generated_ts
        assert again.delay == res.delay


@given(st.binary(max_size=512))
@_seed_examples
def test_relay_line_only_inserts_icao_zeros(data: bytes):
    """ADR-0007: the ONLY difference the relay may introduce is leading zeros
    in field 4 — every other field byte-identical, the address value itself
    unchanged, and the result still inside readsb's line-length window."""
    res = validate_line(data, now=NOW)
    if not res.relay:
        assert res.relay_line is None
        return
    relayed = res.relay_line
    assert relayed is not None
    stripped = data.rstrip(b"\r\n")
    assert MIN_LINE_BYTES <= len(relayed) <= MAX_LINE_BYTES
    before, after = stripped.split(b","), relayed.split(b",")
    assert len(before) == len(after)
    for index, (was, now_field) in enumerate(zip(before, after)):
        if index != 4:
            assert was == now_field
    body_before = before[4][1:] if before[4].startswith(b"~") else before[4]
    body_after = after[4][1:] if after[4].startswith(b"~") else after[4]
    assert after[4].startswith(b"~") is before[4].startswith(b"~")
    assert len(body_after) == 6
    assert int(body_after, 16) == int(body_before, 16)   # value preserved
    assert body_after.endswith(body_before)              # only zeros prepended
    assert res.normalized is (relayed != stripped)


@settings(max_examples=300)
@given(
    base=st.sampled_from(SEEDS),
    position=st.integers(min_value=0, max_value=511),
    replacement=st.integers(min_value=0, max_value=255),
)
def test_single_byte_mutations_never_raise(base: bytes, position: int, replacement: int):
    mutated = bytearray(base)
    mutated[position % len(mutated)] = replacement
    res = validate_line(bytes(mutated), now=NOW)
    assert res.result in RESULT_SET
    if res.relay:
        assert bytes(mutated).rstrip(b"\r\n").split(b",")[0] == b"MSG"


@settings(max_examples=200)
@given(
    base=st.sampled_from(GOLDEN),
    chunk=st.binary(min_size=0, max_size=32),
    position=st.integers(min_value=0, max_value=511),
)
def test_splice_mutations_never_raise(base: bytes, chunk: bytes, position: int):
    cut = position % (len(base) + 1)
    mutated = base[:cut] + chunk + base[cut:]
    res = validate_line(mutated, now=NOW)
    assert res.result in RESULT_SET


@given(st.text(alphabet=st.characters(codec="ascii"), max_size=250))
def test_ascii_text_never_raises(text: str):
    res = validate_line(text.encode("ascii"), now=NOW)
    assert res.result in RESULT_SET


def test_ok_result_never_for_arbitrary_prefix():
    for prefix in (b"SEL", b"ID", b"AIR", b"STA", b"CLK", b"XYZ"):
        line = prefix + GOLDEN[2][3:]
        res = validate_line(line, now=NOW)
        assert res.result != OK
        assert res.relay is False


# --- sec-5: relayed numeric fields must re-parse identically under strtod ---

# C strtod acceptance, including the forms Python float() reads differently
# (or not at all): leading whitespace, hex floats, inf/infinity, nan. Ordered
# so the regex backtracks exactly like strtod (e.g. '5e+' parses as 5).
_STRTOD_SIM_RE = re.compile(
    r"[ \t\n\r\f\v]*[+-]?(?:"
    r"0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)(?:[pP][+-]?[0-9]+)?"
    r"|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    r"|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?"
    r"|[nN][aA][nN]"
    r")"
)

# SBS numeric field indices checked by _ranges_ok (alt, gs, track, lat, lon, vrate)
_NUMERIC_FIELDS = (11, 12, 13, 14, 15, 16)

_NUMERICISH = st.text(
    alphabet="0123456789.eE+-_xXpPinfaNtyINFAY \t", min_size=0, max_size=12)


def _strtod(text: str) -> tuple[float | None, int]:
    """Simulate C strtod: (value, chars consumed); (None, 0) on no conversion."""
    match = _STRTOD_SIM_RE.match(text)
    if match is None:
        return None, 0
    token = match.group(0).lstrip(" \t\n\r\f\v")
    value = float.fromhex(token) if "x" in token.lower() else float(token)
    return value, match.end()


@settings(max_examples=300)
@given(values=st.lists(_NUMERICISH, min_size=6, max_size=6))
def test_relayed_numeric_fields_strtod_identical(values: list[str]):
    fields = [
        "MSG", "3", "1", "103", "3C6589", "10003",
        "2026/08/29", "12:00:00.000", "2026/08/29", "12:00:00.020",
        "", "36000", "", "", "37.82015", "-121.90403", "", "", "0", "0", "0", "0",
    ]
    for index, value in zip(_NUMERIC_FIELDS, values):
        fields[index] = value
    line = ",".join(fields).encode("ascii", errors="replace")
    res = validate_line(line, now=NOW)
    if not res.relay:
        return
    parsed = line.decode("ascii").split(",")
    for index in _NUMERIC_FIELDS:
        field = parsed[index]
        if not field:
            continue
        value, consumed = _strtod(field)
        assert consumed == len(field), (
            f"field {index} {field!r}: strtod stops at {consumed}, readsb would "
            f"read a different value than the validator did")
        assert value == float(field), f"field {index} {field!r} parses differently"


# --- ADR-0004 parity: generated timestamps must equal readsb's computation ---
#
# Reference reimplementation of pinned readsb's SBS timestamp algorithm
# (net_io.c:3141-3161), written independently of the validator: strsep the
# time field at the first '.', strtol the remainder as LITERAL MILLISECONDS,
# glibc strptime("%Y/%m/%d %H:%M:%S") over date + ' ' + prefix (numeric
# fields skip whitespace, consume at most 4/2 digits with glibc's
# stop-early-when-exceeding-range rule, then range-check; trailing garbage
# ignored), then timegm. None == strptime failed == readsb stamps arrival.

_REF_WS = " \t\n\r\f\v"
_REF_STRTOL_RE = re.compile(r"[ \t\n\r\f\v]*[+-]?[0-9]+")


def _ref_get_number(s: str, i: int, lo: int, hi: int, width: int):
    """glibc strptime get_number: (value, next index) or None."""
    while i < len(s) and s[i] in _REF_WS:
        i += 1
    if i >= len(s) or s[i] not in "0123456789":
        return None
    val, n = 0, width
    while True:
        val = val * 10 + ord(s[i]) - 48
        i += 1
        n -= 1
        if n <= 0 or val * 10 > hi or i >= len(s) or s[i] not in "0123456789":
            break
    if val < lo or val > hi:
        return None
    return val, i


def _ref_timegm(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int:
    if y == 0:  # proleptic Gregorian: year 0 == year 400 minus one 400-y cycle
        ordinal = date(400, mo, 1).toordinal() - 146097
    else:
        ordinal = date(y, mo, 1).toordinal()
    days = ordinal - date(1970, 1, 1).toordinal() + d - 1
    return ((days * 24 + h) * 60 + mi) * 60 + s


def readsb_timestamp_ms(date_field: str, time_field: str) -> int | None:
    prefix = time_field.split(".", 1)[0]
    milli = 0
    if "." in time_field:
        matched = _REF_STRTOL_RE.match(time_field.split(".", 1)[1])
        if matched:
            milli = int(matched.group(0))
    s = f"{date_field} {prefix}"
    i, out = 0, []
    for lo, hi, width, delim in ((0, 9999, 4, "/"), (1, 12, 2, "/"),
                                 (1, 31, 2, None), (0, 23, 2, ":"),
                                 (0, 59, 2, ":"), (0, 61, 2, None)):
        got = _ref_get_number(s, i, lo, hi, width)
        if got is None:
            return None
        val, i = got
        out.append(val)
        if delim is not None:
            if i >= len(s) or s[i] != delim:
                return None
            i += 1
    return _ref_timegm(*out) * 1000 + milli


def _ts_line(date_field: str, time_field: str) -> bytes:
    fields = [
        "MSG", "3", "1", "103", "3C6589", "10003",
        date_field, time_field, "2026/08/29", "12:00:00.020",
        "", "36000", "", "", "37.82015", "-121.90403", "", "", "0", "0", "0", "0",
    ]
    return ",".join(fields).encode("ascii", errors="replace")


def _assert_ts_matches_reference(date_field: str, time_field: str) -> None:
    res = validate_line(_ts_line(date_field, time_field), now=NOW)
    ref = readsb_timestamp_ms(date_field, time_field)
    if res.result in (OK, STALE, FUTURE):
        # whenever the guard classified a timestamp, it must be the very value
        # readsb would compute for the same bytes (ADR-0004 stale/future parity)
        assert ref is not None, f"validator parsed what readsb would not: {time_field!r}"
        # sub-ms tolerance: values are ms-quantized; only float64 rounding of
        # far-future years differs between the two computation orders
        assert res.generated_ts == pytest.approx(ref / 1000.0, abs=1e-3)
    elif res.result == UNPARSEABLE_TS:
        assert ref is None, (
            f"validator relayed {date_field!r} {time_field!r} as unparseable but "
            f"readsb would compute {ref} — the stale/future guard was bypassed")


@settings(max_examples=400)
@given(fraction=st.text(alphabet="0123456789+-. abcZ_", max_size=14))
@example(fraction="0000000")
@example(fraction="5")
@example(fraction="3200")
@example(fraction="")
@example(fraction="-5")
@example(fraction="5.3")
def test_fraction_strtol_parity(fraction: str):
    """Arbitrary fractional parts parse as strtol milliseconds, never as a
    decimal fraction, and never as unparseable (the prefix is valid)."""
    time_field = "12:00:00." + fraction
    res = validate_line(_ts_line("2026/08/29", time_field), now=NOW)
    assert res.result in (OK, STALE, FUTURE)  # prefix parses, readsb-style
    ref = readsb_timestamp_ms("2026/08/29", time_field)
    assert ref is not None
    assert res.generated_ts == pytest.approx(ref / 1000.0, abs=1e-3)


@settings(max_examples=400)
@given(
    date_field=st.text(alphabet="0123456789/:-. TZx", max_size=14),
    time_field=st.text(alphabet="0123456789/:.+- abcZ", max_size=18),
)
@example(date_field="2026/08/29", time_field="12:00:00.0000000")
@example(date_field="2026/08/29", time_field="12:00:00Z")
@example(date_field="2026/8/29", time_field="12:0:0")
@example(date_field="2026/08/29", time_field="11:59:79")
@example(date_field="0/1/1", time_field="0:0:0.9")
@example(date_field="2026/02/31", time_field="12:00:61")
@example(date_field="2026/13/29", time_field="12:00:00")
def test_timestamp_parity_with_readsb_reference(date_field: str, time_field: str):
    _assert_ts_matches_reference(date_field, time_field)


@settings(max_examples=400)
@given(
    year=st.integers(min_value=0, max_value=99999),
    month=st.integers(min_value=0, max_value=19),
    day=st.integers(min_value=0, max_value=39),
    hour=st.integers(min_value=0, max_value=30),
    minute=st.integers(min_value=0, max_value=79),
    second=st.integers(min_value=0, max_value=79),
    fraction=st.sampled_from(["", ".", ".000", ".5", ".3200", ".-42", ".abc", "junk"]),
)
def test_structured_timestamp_parity(year, month, day, hour, minute, second, fraction):
    _assert_ts_matches_reference(f"{year}/{month:02d}/{day}",
                                 f"{hour}:{minute}:{second:02d}{fraction}")


# --- sec-4: read-loop framing state machine under segmented byte streams ---

LINE_MAX_BYTES = 256  # small cap keeps oversized-line cases cheap


class _StubRelay:
    """Stub session for _read_loop: records broadcasts, opens no sockets."""

    def __init__(self) -> None:
        self.listening = False
        self.lines: list[bytes] = []

    async def start(self) -> None:
        self.listening = True

    async def stop(self) -> None:
        self.listening = False

    def broadcast(self, line: bytes) -> None:
        self.lines.append(line)


async def _drive_read_loop(chunks: list[bytes]) -> tuple[str, list[bytes], int]:
    cfg = load_config_dict({
        "config_version": 1,
        "adsbhub": {"line_max_bytes": LINE_MAX_BYTES, "idle_timeout": "5s"},
    })
    adapter = Adapter(cfg, clock=lambda: NOW)
    stub = _StubRelay()
    adapter.relay = stub  # type: ignore[assignment]
    reader = asyncio.StreamReader(limit=cfg.adsbhub.line_max_bytes)
    task = asyncio.create_task(adapter._read_loop(reader))
    max_buffer = 0
    for chunk in chunks:
        reader.feed_data(chunk)
        # let the loop drain until the buffer size is stable (a trailing
        # partial line legitimately stays buffered)
        stable, last = 0, -1
        while stable < 5:
            await asyncio.sleep(0)
            size = len(reader._buffer)
            max_buffer = max(max_buffer, size)
            stable, last = (stable + 1, last) if size == last else (0, size)
    reader.feed_eof()
    reason = await asyncio.wait_for(task, 5.0)
    return reason, stub.lines, max_buffer


_FRAME_TOKENS = st.one_of(
    st.binary(max_size=64),
    st.sampled_from(
        [line + b"\r\n" for line in GOLDEN]
        + [GOLDEN[0] + b"\n", b"\r\n", b"\n", b"\r", b"\x00",
           b"M" * (LINE_MAX_BYTES + 50) + b"\n",   # oversized, terminated
           b"M" * (LINE_MAX_BYTES * 3)]            # oversized, unterminated
    ),
)


@settings(max_examples=40, deadline=None)
@given(
    tokens=st.lists(_FRAME_TOKENS, max_size=8),
    cutpoints=st.lists(st.integers(min_value=0, max_value=10_000), max_size=8),
)
def test_read_loop_framing_survives_segmented_streams(tokens, cutpoints):
    """§21.3: arbitrary segmentation (split CRLFs, NULs, oversized lines, EOF
    mid-line) never raises, never grows the reader buffer past the line cap,
    and only relays lines that re-validate as relayable."""
    stream = b"".join(tokens)
    positions = sorted({cut % (len(stream) + 1) for cut in cutpoints} | {0, len(stream)})
    chunks = [stream[a:b] for a, b in zip(positions, positions[1:]) if b > a]

    reason, relayed, max_buffer = asyncio.run(_drive_read_loop(chunks))

    assert reason == "eof"  # no exception escaped the framing loop
    longest_chunk = max((len(chunk) for chunk in chunks), default=0)
    assert max_buffer <= LINE_MAX_BYTES + longest_chunk  # bounded buffering
    for line in relayed:
        again = validate_line(line, now=NOW)
        assert again.relay is True
        assert again.result in (OK, UNPARSEABLE_TS)
