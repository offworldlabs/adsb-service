"""Unit tests for the pure SBS line validator (DESIGN.md §4, §7)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from adsbhub_adapter.sbs import (
    FUTURE,
    INVALID_RANGE,
    INVALID_STRUCTURE,
    OK,
    RESULT_LABELS,
    STALE,
    UNPARSEABLE_TS,
    ValidationResult,
    validate_line,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sbs"

# Reference wall clock: 5 s after the fixtures' generated timestamps.
NOW = datetime(2026, 8, 29, 12, 0, 5, tzinfo=timezone.utc).timestamp()
# make_line()'s generated timestamp (2026/08/29 12:00:00 UTC), Unix seconds.
BASE = NOW - 5.0

CORPUS_EXPECTED = [
    INVALID_STRUCTURE,  # 21 fields
    INVALID_STRUCTURE,  # non-MSG (SEL)
    OK,                 # 5-hex ICAO: zero-padded, ADR-0007
    INVALID_STRUCTURE,  # NUL byte
    INVALID_STRUCTURE,  # > 199 chars
    INVALID_RANGE,      # lat 91
    FUTURE,             # generated 1 h ahead
    STALE,              # generated 2 h ago
    INVALID_STRUCTURE,  # all fields empty
    INVALID_STRUCTURE,  # MSG type 9
    UNPARSEABLE_TS,     # garbage timestamp
    INVALID_RANGE,      # squawk 7A00
    OK,                 # track 360.0: readsb accepts any finite heading (sc-1)
    INVALID_RANGE,      # gs 1501
    INVALID_STRUCTURE,  # < 20 chars
    INVALID_RANGE,      # lowercase callsign
    INVALID_RANGE,      # alt 100001
    INVALID_RANGE,      # vrate -20001
    INVALID_RANGE,      # lon -180.5
    INVALID_RANGE,      # NaN gs
    INVALID_STRUCTURE,  # 7-hex ICAO
    INVALID_STRUCTURE,  # empty ICAO
]


def golden_lines() -> list[bytes]:
    return [(FIXTURES / f"golden_msg_{t}.txt").read_bytes().splitlines()[0] for t in range(1, 9)]


def corpus_lines() -> list[bytes]:
    return [l for l in (FIXTURES / "malformed_corpus.txt").read_bytes().split(b"\n") if l]


def unpadded_lines() -> list[bytes]:
    # real ADSBHub lines whose ICAO field lost its leading zero (ADR-0007)
    return (FIXTURES / "golden_icao_unpadded.txt").read_bytes().splitlines()


def make_line(**overrides: str) -> bytes:
    """A valid MSG,3-shaped line with per-field overrides (0-based indices)."""
    fields = [
        "MSG", "3", "1", "103", "3C6589", "10003",
        "2026/08/29", "12:00:00.000", "2026/08/29", "12:00:00.020",
        "", "36000", "", "", "37.82015", "-121.90403", "", "", "0", "0", "0", "0",
    ]
    for key, value in overrides.items():
        fields[int(key.removeprefix("f"))] = value
    return ",".join(fields).encode("ascii")


def check(line: bytes, **kwargs) -> ValidationResult:
    kwargs.setdefault("now", NOW)
    return validate_line(line, **kwargs)


class TestGolden:
    @pytest.mark.parametrize("msg_type", range(1, 9))
    def test_each_subtype_accepts(self, msg_type: int):
        line = golden_lines()[msg_type - 1]
        res = check(line)
        assert res.result == OK
        assert res.relay is True
        assert res.generated_ts is not None
        assert res.delay is not None and 0 < res.delay < 6

    def test_accepts_with_trailing_crlf(self):
        line = golden_lines()[2]
        assert check(line + b"\r\n").result == OK
        assert check(line + b"\n").result == OK

    def test_pure_and_deterministic(self):
        line = golden_lines()[0]
        assert check(line) == check(line)


class TestCorpus:
    def test_expected_reasons_in_order(self):
        lines = corpus_lines()
        assert len(lines) == len(CORPUS_EXPECTED)
        for n, (line, expected) in enumerate(zip(lines, CORPUS_EXPECTED), 1):
            res = check(line)
            assert res.result == expected, f"corpus line {n}: {res.result} != {expected}"
            assert res.relay is (expected in (OK, UNPARSEABLE_TS))

    def test_five_hex_icao_line_is_padded(self):
        # ADR-0007: corpus line 3 carries a 5-hex ICAO, as ADSBHub emits them
        res = check(corpus_lines()[2])
        assert res.result == OK
        assert res.normalized is True
        assert res.relay_line is not None
        assert res.relay_line.split(b",")[4] == b"0ABC12"


class TestStructure:
    def test_extra_fields_accepted(self):
        # readsb requires >= 22 fields; more is fine
        assert check(make_line() + b",extra").result == OK

    def test_21_fields_rejected(self):
        line = b",".join(make_line().split(b",")[:21])
        assert check(line).result == INVALID_STRUCTURE

    @pytest.mark.parametrize("prefix", [b"SEL", b"ID", b"AIR", b"STA", b"CLK", b"msg", b"MSGX", b""])
    def test_non_msg_rejected(self, prefix: bytes):
        line = prefix + b"," + make_line().split(b",", 1)[1]
        assert check(line).result == INVALID_STRUCTURE

    @pytest.mark.parametrize("msg_type", ["0", "9", "10", "", "A", "3.0", " 3"])
    def test_bad_msg_type_rejected(self, msg_type: str):
        assert check(make_line(f1=msg_type)).result == INVALID_STRUCTURE

    @pytest.mark.parametrize("icao,expected", [
        ("3C6589", OK),
        ("abc123", OK),          # lowercase hex accepted, as in readsb
        ("~3C6589", OK),         # non-ICAO prefix
        ("ABC12", OK),           # short body: zero-padded, ADR-0007
        ("~ABC12", OK),
        ("ABC1234", INVALID_STRUCTURE),
        ("ABC12G", INVALID_STRUCTURE),
        ("", INVALID_STRUCTURE),
        ("~~C6589", INVALID_STRUCTURE),
    ])
    def test_icao_syntax(self, icao: str, expected: str):
        assert check(make_line(f4=icao)).result == expected

    def test_too_short_rejected(self):
        assert check(b"MSG,3,1").result == INVALID_STRUCTURE
        assert check(b"").result == INVALID_STRUCTURE
        assert check(b"\r\n").result == INVALID_STRUCTURE

    def test_length_bounds(self):
        # 199 bytes accepted, 200 rejected (readsb net_io.c:3046-3049)
        base = make_line()
        pad_199 = base + b"," + b"x" * (199 - len(base) - 1)
        assert len(pad_199) == 199
        assert check(pad_199).result == OK
        assert check(pad_199 + b"x").result == INVALID_STRUCTURE

    @pytest.mark.parametrize("bad", [b"\x00", b"\x01", b"\t", b"\x7f", b"\x80", "é".encode()])
    def test_non_printable_rejected(self, bad: bytes):
        line = make_line().replace(b"36000", b"36" + bad + b"00")
        assert check(line).result == INVALID_STRUCTURE

    def test_internal_cr_lf_rejected(self):
        # CR/LF are stripped only at the end; embedded ones are control chars
        assert check(make_line().replace(b"36000", b"36\r00")).result == INVALID_STRUCTURE
        assert check(make_line().replace(b"36000", b"36\n00")).result == INVALID_STRUCTURE


class TestIcaoZeroPadding:
    """ADR-0007: ADSBHub drops the leading zeros of ICAO addresses below
    0x100000, which readsb rejects outright (net_io.c:3112 wants 6-7 chars).
    The adapter left-pads the field to 6 hex chars — the single enumerated
    exception to the DESIGN.md §4 verbatim relay — and nothing else changes."""

    @pytest.mark.parametrize("icao,padded", [
        ("C", "00000C"), ("EC", "0000EC"), ("EEC", "000EEC"),
        ("0EEC", "000EEC"), ("D0EEC", "0D0EEC"),
        ("~C", "~00000C"), ("~ABC", "~000ABC"), ("~ABC12", "~0ABC12"),
        ("d0eec", "0d0eec"),     # case is never touched, only zeros inserted
        ("aB", "0000aB"),
    ])
    def test_short_bodies_are_left_padded(self, icao: str, padded: str):
        line = make_line(f4=icao)
        res = check(line)
        assert res.result == OK
        assert res.relay is True
        assert res.normalized is True
        assert res.relay_line is not None
        assert res.relay_line.split(b",")[4] == padded.encode("ascii")
        # byte-identical except for the inserted zeros
        assert res.relay_line.replace(padded.encode(), icao.encode(), 1) == line

    def test_six_hex_relays_unchanged(self):
        line = make_line(f4="3C6589")
        res = check(line)
        assert res.result == OK
        assert res.normalized is False
        assert res.relay_line == line

    def test_trailing_terminator_is_not_part_of_relay_line(self):
        line = make_line(f4="D0EEC")
        for terminator in (b"\n", b"\r\n"):
            res = check(line + terminator)
            assert res.relay_line is not None
            assert not res.relay_line.endswith(b"\n")
            assert res.relay_line == check(line).relay_line

    @pytest.mark.parametrize("icao", ["ABC1234", "", "~", "G0EEC", "0D0EE C", "~~ABC"])
    def test_structural_rejects_carry_no_relay_line(self, icao: str):
        res = check(make_line(f4=icao))
        assert res.result == INVALID_STRUCTURE
        assert res.relay is False
        assert res.relay_line is None

    def test_padding_past_the_length_cap_is_rejected(self):
        # the inserted zeros push a 199-byte line to 200, which readsb drops
        # (net_io.c:3046-3049) — reject rather than relay bytes it discards
        base = make_line(f4="D0EEC")
        line_199 = base + b"," + b"x" * (199 - len(base) - 1)
        assert len(line_199) == 199
        assert check(line_199).result == INVALID_STRUCTURE
        assert check(line_199[:-1]).result == OK   # 198 -> 199 after padding

    @pytest.mark.parametrize("overrides,expected", [
        ({"f14": "91"}, INVALID_RANGE),
        ({"f6": "2026/08/29", "f7": "10:00:00.000"}, STALE),
        ({"f6": "2026/08/29", "f7": "13:00:00.000"}, FUTURE),
    ])
    def test_padded_but_rejected_lines_still_report_normalized(self, overrides, expected):
        # the padding rule applied, so the counter sees it (ADR-0007): the
        # ratio measures how much of the feed uses the unpadded spelling, not
        # how much of it survived the rest of validation
        res = check(make_line(f4="D0EEC", **overrides))
        assert res.result == expected
        assert res.relay is False
        assert res.relay_line is None
        assert res.normalized is True

    def test_unparseable_ts_line_is_relayed_padded(self):
        res = check(make_line(f4="D0EEC", f6="XXXX/XX/XX", f7="99:99:99.999"))
        assert res.result == UNPARSEABLE_TS
        assert res.relay is True
        assert res.normalized is True
        assert res.relay_line is not None
        assert res.relay_line.split(b",")[4] == b"0D0EEC"

    @pytest.mark.parametrize("icao", ["D0EEC", "3C6589", "~ABC"])
    def test_relay_line_is_a_fixed_point(self, icao: str):
        res = check(make_line(f4=icao))
        assert res.relay_line is not None
        again = check(res.relay_line)
        assert again.result == res.result
        assert again.relay is True
        assert again.relay_line == res.relay_line
        assert again.normalized is False
        assert again.generated_ts == res.generated_ts
        assert again.delay == res.delay

    def test_golden_unpadded_capture(self):
        # real ADSBHub bytes, 2026-09-05 (see fixtures README); NOW is 5 s
        # after their generated timestamp
        now = datetime(2026, 9, 5, 22, 4, 59, tzinfo=timezone.utc).timestamp()
        lines = unpadded_lines()
        assert len(lines) == 3
        for line in lines:
            res = check(line, now=now)
            assert res.result == OK
            assert res.relay is True
            assert res.normalized is True
            assert res.relay_line is not None
            assert res.relay_line.split(b",")[4] == b"0D0EEC"
            assert res.relay_line.replace(b"0D0EEC", b"D0EEC", 1) == line
            # one pass is enough: revalidating the relayed bytes changes nothing
            again = check(res.relay_line, now=now)
            assert (again.result, again.relay, again.relay_line, again.normalized) == (
                res.result, True, res.relay_line, False)
            assert again.generated_ts == res.generated_ts and again.delay == res.delay


class TestRanges:
    @pytest.mark.parametrize("field,value,expected", [
        # latitude (f14)
        ("f14", "90", OK), ("f14", "-90", OK), ("f14", "90.0001", INVALID_RANGE),
        ("f14", "-90.0001", INVALID_RANGE), ("f14", "", OK),
        # longitude (f15)
        ("f15", "180", OK), ("f15", "-180", OK), ("f15", "180.5", INVALID_RANGE),
        ("f15", "-180.5", INVALID_RANGE),
        # baro altitude (f11)
        ("f11", "-2000", OK), ("f11", "100000", OK),
        ("f11", "-2001", INVALID_RANGE), ("f11", "100001", INVALID_RANGE),
        # ground speed (f12)
        ("f12", "0", OK), ("f12", "1500", OK),
        ("f12", "-0.1", INVALID_RANGE), ("f12", "1500.1", INVALID_RANGE),
        # track (f13): any finite value readsb itself accepts (net_io.c:3207-3216),
        # bounded only against extreme parser overflow (spec §12.2, sc-1)
        ("f13", "0", OK), ("f13", "359.99", OK),
        ("f13", "360", OK), ("f13", "-0.1", OK), ("f13", "-90", OK),
        ("f13", "3600", OK), ("f13", "-3600", OK),
        ("f13", "3600.1", INVALID_RANGE), ("f13", "-3600.1", INVALID_RANGE),
        ("f13", "1e10", INVALID_RANGE), ("f13", "inf", INVALID_RANGE),
        # vertical rate (f16)
        ("f16", "20000", OK), ("f16", "-20000", OK),
        ("f16", "20001", INVALID_RANGE), ("f16", "-20001", INVALID_RANGE),
        # squawk (f17): exactly four octal digits
        ("f17", "0000", OK), ("f17", "7777", OK), ("f17", "7778", INVALID_RANGE),
        ("f17", "777", INVALID_RANGE), ("f17", "77777", INVALID_RANGE),
        ("f17", "12a4", INVALID_RANGE), ("f17", "", OK),
        # non-finite / unparseable numerics (spec §12.1)
        ("f12", "nan", INVALID_RANGE), ("f12", "inf", INVALID_RANGE),
        ("f13", "NaN", INVALID_RANGE), ("f11", "1e999", INVALID_RANGE),
        ("f11", "12x", INVALID_RANGE),
        # sec-5: float() accepts these, C strtod would read something else
        ("f11", "3_600", INVALID_RANGE),   # '_' separator: strtod stops at '_'
        ("f11", "0x10", INVALID_RANGE),    # hex float form
        ("f11", " 500", INVALID_RANGE),    # whitespace (fields are comma-split)
        ("f11", "500 ", INVALID_RANGE),
        ("f12", "Infinity", INVALID_RANGE),
        ("f12", "5e", INVALID_RANGE),      # strtod backtracks to 5, float() raises
        # strtod-identical forms stay accepted
        ("f11", "+500", OK), ("f11", "500.", OK), ("f11", ".5", OK),
        ("f11", "5e2", OK), ("f11", "5E+2", OK), ("f11", "-1.5e2", OK),
    ])
    def test_numeric_ranges(self, field: str, value: str, expected: str):
        assert check(make_line(**{field: value})).result == expected

    def test_altitude_bounds_configurable(self):
        # sc-3: spec §12.2 marks the altitude window configurable
        line_high = make_line(f11="120000")
        assert check(line_high).result == INVALID_RANGE
        assert check(line_high, altitude_max_ft=150000.0).result == OK
        line_low = make_line(f11="-2500")
        assert check(line_low).result == INVALID_RANGE
        assert check(line_low, altitude_min_ft=-3000.0).result == OK
        assert check(make_line(f11="36000"), altitude_max_ft=30000.0).result == INVALID_RANGE

    @pytest.mark.parametrize("callsign,expected", [
        ("UAL2143 ", OK),            # right padding trimmed
        ("AB CD", OK),               # internal space preserved (spec §12.1)
        ("A" * 16, OK),
        ("A" * 17, INVALID_RANGE),
        ("ual123", INVALID_RANGE),   # printable subset is A-Z/0-9/space
        ("UA-123", INVALID_RANGE),
        ("", OK),                    # absent
        ("        ", OK),            # padding only -> absent
    ])
    def test_callsign(self, callsign: str, expected: str):
        assert check(make_line(f10=callsign)).result == expected

    def test_empty_fields_are_absent_not_zero(self):
        line = make_line(f11="", f12="", f13="", f14="", f15="", f16="", f17="")
        assert check(line).result == OK


class TestTimestampGuard:
    def _line_at(self, dt: datetime) -> bytes:
        return make_line(f6=dt.strftime("%Y/%m/%d"), f7=dt.strftime("%H:%M:%S.%f")[:-3])

    def test_exact_now(self):
        res = check(self._line_at(datetime.fromtimestamp(NOW, tz=timezone.utc)))
        assert res.result == OK
        assert res.delay == pytest.approx(0.0, abs=0.001)

    def test_future_tolerance_boundary(self):
        base = datetime.fromtimestamp(NOW, tz=timezone.utc)
        # exactly +1 s ahead: not > tolerance -> ok
        res = check(self._line_at(base + timedelta(seconds=1)))
        assert res.result == OK
        assert res.delay == pytest.approx(-1.0, abs=0.001)
        # just past the tolerance -> future
        res = check(self._line_at(base + timedelta(milliseconds=1001)))
        assert res.result == FUTURE
        assert res.relay is False

    def test_stale_boundary(self):
        base = datetime.fromtimestamp(NOW, tz=timezone.utc)
        assert check(self._line_at(base - timedelta(seconds=60))).result == OK
        res = check(self._line_at(base - timedelta(seconds=60, milliseconds=1)))
        assert res.result == STALE
        assert res.relay is False
        assert res.delay == pytest.approx(60.001, abs=0.001)

    def test_custom_tolerances(self):
        base = datetime.fromtimestamp(NOW, tz=timezone.utc)
        line = self._line_at(base - timedelta(seconds=30))
        assert check(line).result == OK
        assert check(line, max_record_age=10.0).result == STALE
        ahead = self._line_at(base + timedelta(seconds=3))
        assert check(ahead).result == FUTURE
        assert check(ahead, future_tolerance=5.0).result == OK

    def test_no_fractional_seconds_accepted(self):
        res = check(make_line(f7="12:00:00"))
        assert res.result == OK

    @pytest.mark.parametrize("date,time", [
        ("", ""), ("2026/08/29", ""), ("", "12:00:00.000"),
        ("XXXX/XX/XX", "99:99:99.999"), ("2026-08-29", "12:00:00.000"),
        ("2026/13/29", "12:00:00.000"), ("2026/08/29", "25:00:00.000"),
        # glibc consumes seconds '68' fully (6 then 8) and range-fails, as
        # does minute '65' — matching readsb's strptime failure -> arrival
        ("2026/08/29", "12:00:68.000"), ("2026/08/29", "12:65:00.000"),
        ("2026/+8/29", "12:00:00.000"), ("12345/08/29", "12:00:00.000"),
    ])
    def test_unparseable_relayed_but_counted(self, date: str, time: str):
        res = check(make_line(f6=date, f7=time))
        assert res.result == UNPARSEABLE_TS
        assert res.relay is True
        assert res.generated_ts is None and res.delay is None

    def test_delay_feeds_histogram_source(self):
        res = check(golden_lines()[3])
        assert res.delay == pytest.approx(NOW - res.generated_ts)


class TestReadsbTimestampParity:
    """The validator must read fields 7/8 EXACTLY as pinned readsb does
    (net_io.c:3135-3161): glibc strptime('%Y/%m/%d %H:%M:%S') on the part
    before the first '.', timegm (UTC), plus strtol(remainder) taken as a
    literal integer count of milliseconds — never as a decimal fraction.
    Otherwise the ADR-0004 stale/future guard judges a different timestamp
    than readsb stamps on the relayed bytes."""

    @pytest.mark.parametrize("fraction,millis", [
        ("000", 0),        # canonical 3-digit form
        ("0000000", 0),    # NOT a %f-style fraction: strtol('0000000') = 0
        ("5", 5),          # 5 ms, not 500 ms
        ("32", 32),
        ("320", 320),
        ("3200", 3200),    # 3.2 s, not 0.32 s
        ("", 0),           # trailing '.' with nothing after
        ("abc", 0),        # strtol: no digits -> 0
        ("-5", -5),        # strtol accepts a sign
        ("+9", 9),
        ("5.3", 5),        # strtol stops at the second '.'
        (" 7x", 7),        # strtol skips leading whitespace, stops at 'x'
        ("- 5", 0),        # sign then non-digit: no conversion
    ])
    def test_fraction_is_literal_milliseconds(self, fraction: str, millis: int):
        res = check(make_line(f7=f"12:00:00.{fraction}"))
        assert res.generated_ts == pytest.approx(BASE + millis / 1000.0, abs=1e-6)
        assert res.delay == pytest.approx(5.0 - millis / 1000.0, abs=1e-6)
        assert res.result == OK

    def test_no_fraction_means_zero_ms(self):
        res = check(make_line(f7="12:00:00"))
        assert res.result == OK
        assert res.generated_ts == pytest.approx(BASE, abs=1e-6)

    def test_stale_line_with_long_zero_fraction_rejected(self):
        # THE ADR-0004 bypass this parity exists to close: a 30-min-old line
        # whose '.0000000' fraction a %f-based parser called unparseable (and
        # relayed) while readsb parsed the real stale timestamp and clamped
        # it to arrival — serving stale data as fresh.
        old = datetime.fromtimestamp(NOW - 1800, tz=timezone.utc)
        res = check(make_line(f6=old.strftime("%Y/%m/%d"),
                              f7=old.strftime("%H:%M:%S") + ".0000000"))
        assert res.result == STALE
        assert res.relay is False
        assert res.delay == pytest.approx(1800.0, abs=0.001)

    def test_large_fraction_can_push_into_future(self):
        # 12:00:04 + 2500 ms = 12:00:06.5 -> 1.5 s ahead of NOW -> future
        res = check(make_line(f7="12:00:04.2500"))
        assert res.result == FUTURE
        assert res.generated_ts == pytest.approx(BASE + 6.5, abs=1e-6)

    def test_negative_fraction_subtracts(self):
        # 12:00:06 - 5 ms = 12:00:05.995 -> 0.995 s ahead: within tolerance
        res = check(make_line(f7="12:00:06.-5"))
        assert res.result == OK
        assert res.generated_ts == pytest.approx(BASE + 5.995, abs=1e-6)
        # 12:00:07 - 500 ms = 12:00:06.5 -> 1.5 s ahead -> future
        assert check(make_line(f7="12:00:07.-500")).result == FUTURE

    @pytest.mark.parametrize("date,time,offset", [
        ("2026/08/29", "12:00:00Z", 0.0),        # trailing garbage ignored
        ("2026/08/29", "12:00:00junk", 0.0),
        ("2026/8/29", "12:0:0", 0.0),            # single-digit fields
        (" 2026/ 08/ 29", " 12: 00: 00", 0.0),   # strptime skips whitespace
        ("2026/08/29", "11:59:79", -53.0),       # glibc stops early: S=7, '9' is garbage
        ("2026/08/2912:00:00", "", 0.0),         # date field carrying the time
    ])
    def test_glibc_strptime_leniency(self, date: str, time: str, offset: float):
        res = check(make_line(f6=date, f7=time))
        assert res.result == OK
        assert res.generated_ts == pytest.approx(BASE + offset, abs=1e-6)

    def test_leap_second_and_day_rollover(self):
        # strptime allows S up to 61 and never cross-checks the calendar;
        # timegm rolls both forward, exactly like readsb
        res = check(make_line(f7="11:59:60"))
        assert res.result == OK
        assert res.generated_ts == pytest.approx(BASE, abs=1e-6)


class TestContract:
    def test_result_labels_exact_set(self):
        assert RESULT_LABELS == (OK, "invalid_structure", "invalid_range",
                                 "stale", "future", "unparseable_ts")

    def test_relay_only_for_ok_and_unparseable(self):
        for line in golden_lines() + corpus_lines():
            res = check(line)
            assert res.relay is (res.result in (OK, UNPARSEABLE_TS))

    def test_relay_line_set_exactly_when_relayed(self):
        for line in golden_lines() + corpus_lines() + unpadded_lines():
            for candidate in (line, line + b"\r\n"):
                res = check(candidate)
                assert (res.relay_line is not None) is res.relay
                if res.relay and not res.normalized:
                    assert res.relay_line == candidate.rstrip(b"\r\n")

    def test_non_bytes_input_does_not_raise(self):
        assert validate_line("MSG,not,bytes", now=NOW).result == INVALID_STRUCTURE  # type: ignore[arg-type]
        assert validate_line(None, now=NOW).result == INVALID_STRUCTURE  # type: ignore[arg-type]
