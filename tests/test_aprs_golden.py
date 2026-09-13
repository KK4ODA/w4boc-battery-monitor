"""Golden tests for the APRS wire formats.

Every expected string below was copied from logs/monitor.log on the W4BOC PC,
where these exact frames have been decoding correctly on aprs.fi (RF via
soundmodem AGWPE + APRS-IS) since April 2026. If one of these tests fails,
the telemetry format changed — do not "fix" the test.
"""
import asyncio
import struct
from datetime import datetime, timedelta, timezone

import pytest

from w4boc import agwpe, aprs, config


def _now_iso(delta_s: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_s)).isoformat(timespec="seconds")


def bms_sample(v=14.4, i=-4.0, soc=100, t=23.5, age_s=0, **kw):
    d = {"pack_voltage": v, "pack_current": i, "soc_pct": soc, "temp_c": t,
         "charge_fet": 1, "discharge_fet": 1, "protections": [], "ts": _now_iso(age_s)}
    d.update(kw)
    return d


def chg_sample(i=3.4, age_s=0, error="NO_ERROR"):
    return {"voltage": 13.8, "current": i, "state": "STORAGE", "error": error, "ts": _now_iso(age_s)}


# ---- headers (from log 2026-09-13 05:23:16..19) ----

def test_header_frames_exact():
    assert aprs.build_header_infos() == [
        ":W4BOC-1  :PARM.Vbat,Ibat,SoC,Tbat,IchgO,ChgFET,DisFET,Mains,BMSok,ProtOK,ChgOK,Ok50,Ok30",
        ":W4BOC-1  :UNIT.Vdc,Adc,Pct,degC,Adc,on,on,ok,ok,ok,ok,ok,ok",
        ":W4BOC-1  :EQNS.0,0.1,0,0,1,-128,0,1,0,0,0.5,-20,0,0.2,0",
        ":W4BOC-1  :BITS.11111111,W4BOC Battery",
    ]


def test_tocall_and_path_unchanged():
    assert aprs.TOCALL == "APZBAT"
    assert config.APRS_CALLSIGN == "W4BOC-1"
    assert config.APRS_PATH == "WIDE1-1,WIDE2-1"


# ---- data frames (from log 2026-09-13 04:53:46: T#021,144,124,100,087,017,11111111) ----

def test_data_frame_exact():
    info = aprs.build_data_info(21, bms_sample(14.4, -4.0, 100, 23.5), chg_sample(3.4))
    assert info == "T#021,144,124,100,087,017,11111111"


def test_data_frame_seq_wraps_at_1000():
    assert aprs.build_data_info(1021, bms_sample(), chg_sample()).startswith("T#021,")


def test_data_frame_no_samples():
    # No BMS -> zero volts, 0 A (=128), 0 %, -20 C (=40); no charger -> 0.
    # Bits: FETs/mains/BMSok unknown -> 0; "no protections/error/low SoC" -> 1 (v1 behaviour)
    info = aprs.build_data_info(5, None, None)
    assert info == "T#005,000,128,000,040,000,00001111"


def test_analog_encoding_clamps():
    assert aprs.encode_analog(30.0, 0, 0.1, 0) == 255
    assert aprs.encode_analog(-200, 0, 1, -128) == 0
    assert aprs.encode_analog(None, 0, 1, 0) == 0


# ---- bits ----

def test_bits_all_healthy():
    assert aprs._build_bits(bms_sample(), chg_sample()) == "11111111"


def test_bits_mains_uses_fast_window_and_detector():
    fresh = chg_sample(age_s=3 * 60)          # 3 min old: stale for v1 (2 min), fresh for v2 (5 min)
    assert aprs._build_bits(bms_sample(), fresh)[2] == "1"
    stale = chg_sample(age_s=config.MAINS_FAST_MINUTES * 60 + 5)
    assert aprs._build_bits(bms_sample(), stale)[2] == "0"
    # detector says lost -> bit 3 off even with a recent sample
    assert aprs._build_bits(bms_sample(), chg_sample(), mains_lost=True)[2] == "0"
    # everything else untouched by the mains flag
    assert aprs._build_bits(bms_sample(), chg_sample(), mains_lost=True) == "11011111"


def test_bits_bms_stale_and_thresholds():
    bits = aprs._build_bits(bms_sample(age_s=130), chg_sample())
    assert bits[3] == "0"
    bits = aprs._build_bits(bms_sample(soc=45), chg_sample())
    assert bits[6] == "0" and bits[7] == "1"
    bits = aprs._build_bits(bms_sample(soc=20), chg_sample())
    assert bits[6] == "0" and bits[7] == "0"
    bits = aprs._build_bits(bms_sample(protections=["cell_under_voltage"], charge_fet=0), chg_sample(error="LOW_BATTERY"))
    assert bits == "01110011"


# ---- position (from log: =3348.33NI08408.79W#W4BOC batt 14.37V 100% 23C) ----

def test_position_exact():
    assert aprs.build_position_info(bms_sample(14.37, 0.0, 100, 23.0)) == \
        "=3348.33NI08408.79W#W4BOC batt 14.37V 100% 23C"


def test_position_no_bms():
    assert aprs.build_position_info(None) == "=3348.33NI08408.79W#W4BOC battery monitor"


def test_position_mains_lost_suffix():
    p = aprs.build_position_info(bms_sample(13.42, -4.6, 96, 22.0), mains_lost=True)
    assert p == "=3348.33NI08408.79W#W4BOC batt 13.42V 96% 22C MAINS LOST"
    # comment never exceeds 43 chars after the position (18 chars incl. '=')
    assert len(p) <= 1 + 19 + 43
    # and with mains on the v1 format is untouched
    assert aprs.build_position_info(bms_sample(13.42, -4.6, 96, 22.0), mains_lost=False) == \
        "=3348.33NI08408.79W#W4BOC batt 13.42V 96% 22C"


def test_latlon_format():
    assert aprs.latlon_aprs(33.805520, -84.146500) == "3348.33NI08408.79W#"


# ---- status ----

def test_status_packet():
    assert aprs.build_status_info("AC MAINS LOST 13.42V 96% -4.6A") == ">AC MAINS LOST 13.42V 96% -4.6A"
    assert aprs.build_status_info("a|b~c") == ">a/b-c"
    assert len(aprs.build_status_info("x" * 100)) == 63


# ---- APRS-IS line ----

def test_tnc2_line():
    assert aprs.tnc2_for_aprsis("T#001,000,128,000,040,000,00011111") == \
        "W4BOC-1>APZBAT:T#001,000,128,000,040,000,00011111\r\n"


# ---- AGWPE binary frames ----

def test_agw_header_layout():
    h = agwpe._header("V", 0xF0, "W4BOC-1", "APZBAT", 42)
    assert len(h) == 36
    port, kind, _, pid, _, cfrom, cto, dlen, user = struct.unpack("<I B B B B 10s 10s I I", h)
    assert (port, chr(kind), pid, dlen, user) == (0, "V", 0xF0, 42, 0)
    assert cfrom == b"W4BOC-1\x00\x00\x00"
    assert cto == b"APZBAT\x00\x00\x00\x00"


def test_agw_send_ui_wire_bytes():
    """Spin up a fake AGWPE server and check the exact bytes: an 'X' register
    frame, then a 'V' frame = header + via-count + 10-byte vias + info."""
    received = bytearray()

    async def run():
        async def handle(reader, writer):
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                received.extend(data)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = agwpe.AgwClient("127.0.0.1", port, "W4BOC-1")
        ok = await client.send_ui("APZBAT", "WIDE1-1,WIDE2-1", "T#001,000,128,000,040,000,00011111")
        assert ok
        await asyncio.sleep(0.2)
        client._writer.close()
        await asyncio.sleep(0.1)
        server.close()
        await server.wait_closed()

    asyncio.run(run())

    reg = agwpe._header("X", 0, "W4BOC-1", "", 0)
    assert bytes(received[:36]) == reg
    data = b"\x02" + b"WIDE1-1".ljust(10, b"\x00") + b"WIDE2-1".ljust(10, b"\x00") \
        + b"T#001,000,128,000,040,000,00011111"
    frame = agwpe._header("V", 0xF0, "W4BOC-1", "APZBAT", len(data)) + data
    assert bytes(received[36:]) == frame


def test_agw_unreachable_is_nonfatal():
    async def run():
        client = agwpe.AgwClient("127.0.0.1", 1, "W4BOC-1")   # nothing listens on port 1
        return await client.send_ui("APZBAT", "", "x")
    assert asyncio.run(run()) is False
