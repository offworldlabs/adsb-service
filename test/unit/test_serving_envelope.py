"""jv2 envelope fixture shape: the fields the tar1090-node proxy maps.

plan §0 req 4 (proxy expectations, tar1090-node v0.2.0 proxy/server.js:90-129):
``ac`` array, integer millisecond ``now``, ``alt_baro`` as the string
"ground" for on-ground aircraft, ``seen_pos`` in float seconds — and, for
SBS-derived aircraft, NO ``alt_geom`` key (SBS carries no geometric altitude,
plan §0 req 3; never synthesize it, spec §33.9).

Field inventory per code-adsblol-api.md facts 1-envelope/4-fields.
"""

from __future__ import annotations

import json
import re

import pytest

from test.unit.test_serving_common import FIXTURE_PATH

# The full per-aircraft field set expected for an SBS-derived circle-query hit
# (json_out.c printMode 0 plus the dst/dir keys api.c:755-758 appends).
SBS_AIRCRAFT_FIELDS = {
    "hex",
    "type",
    "flight",
    "alt_baro",
    "gs",
    "track",
    "baro_rate",
    "squawk",
    "lat",
    "lon",
    "nic",
    "rc",
    "seen_pos",
    "alert",
    "spi",
    "mlat",
    "tisb",
    "messages",
    "seen",
    "rssi",
    "dst",
    "dir",
}


@pytest.fixture(scope="module")
def envelope() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_top_level_shape(envelope: dict) -> None:
    assert set(envelope) == {"ac", "msg", "now", "total", "ctime", "ptime"}
    assert isinstance(envelope["ac"], list)
    assert envelope["msg"] == "No error"
    assert envelope["total"] == len(envelope["ac"])
    assert isinstance(envelope["ptime"], int)


def test_now_is_integer_milliseconds(envelope: dict) -> None:
    now = envelope["now"]
    assert isinstance(now, int) and not isinstance(now, bool)
    # ms-scale epoch, not seconds: 2001-09-09..5138-11-16 in ms.
    assert 10**12 < now < 10**14
    # api.c:771,773: now and ctime are the same cache-generation timestamp.
    assert envelope["ctime"] == now


def test_airborne_sbs_aircraft_full_field_set(envelope: dict) -> None:
    airborne = [a for a in envelope["ac"] if a["alt_baro"] != "ground"]
    assert airborne, "fixture must contain an airborne aircraft"
    for aircraft in airborne:
        assert set(aircraft) == SBS_AIRCRAFT_FIELDS
        assert isinstance(aircraft["alt_baro"], int) and not isinstance(aircraft["alt_baro"], bool)


def test_no_alt_geom_for_sbs_aircraft(envelope: dict) -> None:
    for aircraft in envelope["ac"]:
        assert "alt_geom" not in aircraft


def test_ground_aircraft_alt_baro_is_ground_string(envelope: dict) -> None:
    ground = [a for a in envelope["ac"] if a["alt_baro"] == "ground"]
    assert len(ground) == 1
    aircraft = ground[0]
    assert isinstance(aircraft["alt_baro"], str)
    assert aircraft["alt_baro"] == "ground"
    assert "alt_geom" not in aircraft


def test_seen_pos_is_float_seconds(envelope: dict) -> None:
    for aircraft in envelope["ac"]:
        seen_pos = aircraft["seen_pos"]
        assert isinstance(seen_pos, float)
        # positions older than TRACK_EXPIRE (60 s) never match circle queries
        assert 0.0 <= seen_pos < 60.0
        seen = aircraft["seen"]
        assert isinstance(seen, (int, float)) and not isinstance(seen, bool)
        assert seen >= 0.0


def test_aircraft_field_types(envelope: dict) -> None:
    for aircraft in envelope["ac"]:
        assert re.fullmatch(r"[0-9a-f]{6}", aircraft["hex"])
        assert aircraft["type"] == "other"  # SBS-derived, no addr-type info
        assert isinstance(aircraft["flight"], str)
        assert re.fullmatch(r"[0-7]{4}", aircraft["squawk"])
        assert -90.0 <= aircraft["lat"] <= 90.0
        assert -180.0 <= aircraft["lon"] <= 180.0
        # SBS carries no NIC/Rc integrity data
        assert aircraft["nic"] == 0
        assert aircraft["rc"] == 0
        assert aircraft["mlat"] == []
        assert aircraft["tisb"] == []
        assert isinstance(aircraft["messages"], int)
        assert isinstance(aircraft["rssi"], (int, float))
        assert aircraft["alert"] in (0, 1)
        assert aircraft["spi"] in (0, 1)
        # circle queries append dst (nm) / dir (deg true); readsb's rounded
        # bearing was observed live to reach 360.0 inclusive
        assert isinstance(aircraft["dst"], (int, float))
        assert isinstance(aircraft["dir"], (int, float))
        assert 0.0 <= aircraft["dir"] <= 360.0


def test_observation_epoch_reconstruction(envelope: dict) -> None:
    # blah2-api reconstructs observation time as now - seen_pos*1000; the
    # arithmetic must land at a plausible ms epoch for every aircraft.
    for aircraft in envelope["ac"]:
        observation_ms = envelope["now"] - aircraft["seen_pos"] * 1000.0
        assert 10**12 < observation_ms < 10**14
        assert observation_ms <= envelope["now"]
