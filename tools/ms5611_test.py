#!/usr/bin/env python3
"""Host test for components/ms5611/ms5611_calc.c - PROM CRC and the
pressure/temperature compensation.

Same trick as tools/host_c.py: the file is freestanding, so clang compiles
the exact code that flies and ctypes drives it.

The reference numbers are external, never the C's own output:
  * TE application note AN520's CRC example, whose answer is published
  * the MS5611-01BA03 datasheet's worked compensation example, whose
    expected TEMP and P are printed in the datasheet
  * the second-order branch re-implemented here in Python straight from
    the datasheet text, so the C is checked against the spec rather than
    against itself

MS5611_SRC points the compiler at a doctored copy for mutation runs.

Run:  python tools/ms5611_test.py
"""

import ctypes
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.environ.get("MS5611_SRC") or os.path.join(ROOT, "components", "ms5611")

SHIM = r"""
int _fltused = 0;
#include "ms5611_calc.c"
#define EXPORT __declspec(dllexport)
EXPORT unsigned char m_crc4(const unsigned short *p){ return ms5611_prom_crc4(p); }
EXPORT int  m_crcok(const unsigned short *p){ return ms5611_prom_crc_ok(p); }
EXPORT void m_unpack(ms5611_cal_t *c, const unsigned short *p){ ms5611_cal_from_prom(c,p); }
EXPORT int  m_plaus(const ms5611_cal_t *c){ return ms5611_cal_plausible(c); }
EXPORT void m_comp(const ms5611_cal_t *c, unsigned int d1, unsigned int d2,
                   int *t, int *p){ ms5611_compensate(c,d1,d2,t,p); }
EXPORT int  m_rplaus(int t, int p){ return ms5611_reading_plausible(t,p); }
EXPORT int  m_size(void){ return (int)sizeof(ms5611_cal_t); }
"""


class CCal(ctypes.Structure):
    _fields_ = [("prom", ctypes.c_uint16 * 8)] + \
               [(n, ctypes.c_uint16) for n in ("c1", "c2", "c3", "c4", "c5", "c6")]


class Host:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        shim = os.path.join(self._tmp.name, "shim.c")
        with open(shim, "w") as fh:
            fh.write(SHIM)
        dll = os.path.join(self._tmp.name, "ms.dll")
        cmd = ["clang", "-shared", "-nostdlib", "-Wl,-noentry", "-O1",
               "-I", SRC, shim, "-o", dll]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr)
            raise SystemExit("clang failed - this host test needs clang on PATH")
        self.lib = ctypes.CDLL(dll)
        L = self.lib
        u16p, pc, ip = (ctypes.POINTER(ctypes.c_uint16),
                        ctypes.POINTER(CCal), ctypes.POINTER(ctypes.c_int))
        L.m_crc4.argtypes = [u16p]; L.m_crc4.restype = ctypes.c_uint8
        L.m_crcok.argtypes = [u16p]; L.m_crcok.restype = ctypes.c_int
        L.m_unpack.argtypes = [pc, u16p]
        L.m_plaus.argtypes = [pc]; L.m_plaus.restype = ctypes.c_int
        L.m_comp.argtypes = [pc, ctypes.c_uint32, ctypes.c_uint32, ip, ip]
        L.m_rplaus.argtypes = [ctypes.c_int, ctypes.c_int]
        L.m_rplaus.restype = ctypes.c_int
        L.m_size.restype = ctypes.c_int
        if L.m_size() != ctypes.sizeof(CCal):
            raise SystemExit(f"ms5611_cal_t layout mismatch: C {L.m_size()} B, "
                             f"ctypes {ctypes.sizeof(CCal)} B")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        h = self.lib._handle
        del self.lib
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(h))
        self._tmp.cleanup()

    def arr(self, words):
        return (ctypes.c_uint16 * 8)(*words)

    def cal(self, words):
        c = CCal()
        self.lib.m_unpack(ctypes.byref(c), self.arr(words))
        return c

    def comp(self, c, d1, d2):
        t, p = ctypes.c_int(), ctypes.c_int()
        self.lib.m_comp(ctypes.byref(c), d1, d2, ctypes.byref(t), ctypes.byref(p))
        return t.value, p.value


def py_compensate(c1, c2, c3, c4, c5, c6, d1, d2):
    """The datasheet compensation, re-implemented here from the spec text so
    the C is checked against the specification and not against itself."""
    dT = d2 - (c5 << 8)
    temp = 2000 + ((dT * c6) >> 23)
    off = (c2 << 16) + ((c4 * dT) >> 7)
    sens = (c1 << 15) + ((c3 * dT) >> 8)
    if temp < 2000:
        t_off = temp - 2000
        t2 = (dT * dT) >> 31
        off2 = (5 * t_off * t_off) >> 1
        sens2 = (5 * t_off * t_off) >> 2
        if temp < -1500:
            t_frz = temp + 1500
            off2 += 7 * t_frz * t_frz
            sens2 += (11 * t_frz * t_frz) >> 1
        temp -= t2
        off -= off2
        sens -= sens2
    p = (((d1 * sens) >> 21) - off) >> 15
    return temp, p


# The MS5611-01BA03 datasheet's worked example.
DS = dict(c1=40127, c2=36924, c3=23317, c4=23282, c5=33464, c6=28312,
          d1=9085466, d2=8569150)

FAIL = 0


def check(name, got, want):
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"         got {got!r}  want {want!r}")


def main():
    with Host() as H:
        print("\nPROM CRC - TE application note AN520")
        # The published example block. Its correct CRC is 0xB.
        an520 = [0x3132, 0x3334, 0x3536, 0x3738, 0x3940, 0x4142, 0x4344, 0x450B]
        check("AN520 example block -> 0xB", H.lib.m_crc4(H.arr(an520)), 0x0B)
        check("     and validates", H.lib.m_crcok(H.arr(an520)), 1)

        # The CRC nibble in word 7 must be excluded from its own computation:
        # changing only that nibble must not change the computed CRC.
        for nib in (0x0, 0x5, 0xF):
            blk = an520[:7] + [0x4500 | nib]
            check(f"     CRC nibble {nib:#x} excluded from its own input",
                  H.lib.m_crc4(H.arr(blk)), 0x0B)

        print("\nCRC catches corruption - one bit at a time")
        caught = 0
        for w in range(7):
            for bit in range(16):
                bad = list(an520)
                bad[w] ^= (1 << bit)
                if not H.lib.m_crcok(H.arr(bad)):
                    caught += 1
        check("all 112 single-bit flips in C1..C6 rejected", caught, 112)

        print("\ncompensation - the datasheet's own worked example")
        prom = [0, DS["c1"], DS["c2"], DS["c3"], DS["c4"], DS["c5"], DS["c6"], 0]
        cal = H.cal(prom)
        t, p = H.comp(cal, DS["d1"], DS["d2"])
        check("TEMP = 2007 (20.07 C)", t, 2007)
        check("P    = 100009 Pa (1000.09 mbar)", p, 100009)
        check("     matches the spec re-implemented in Python",
              (t, p), py_compensate(DS["c1"], DS["c2"], DS["c3"], DS["c4"],
                                    DS["c5"], DS["c6"], DS["d1"], DS["d2"]))

        print("\nsecond-order branch - the half the datasheet example never runs")
        # Walk D2 down to drive TEMP below 20 C, then below -15 C, and check
        # against the Python re-implementation at every step.
        agree = below20 = belowm15 = 0
        for d2 in range(7_000_000, 8_700_000, 25_000):
            t, p = H.comp(cal, DS["d1"], d2)
            wt, wp = py_compensate(DS["c1"], DS["c2"], DS["c3"], DS["c4"],
                                   DS["c5"], DS["c6"], DS["d1"], d2)
            if (t, p) == (wt, wp):
                agree += 1
            if t < 2000:
                below20 += 1
            if t < -1500:
                belowm15 += 1
        check("every sample agrees with the spec", agree, 68)
        check("     and the run really did cross 20 C", below20 > 20, True)
        check("     and really did cross -15 C", belowm15 > 5, True)

        print("\ncalibration plausibility - CRC alone is not enough")
        zero = [0] * 8
        check("an all-zero PROM has a VALID crc of 0", H.lib.m_crcok(H.arr(zero)), 1)
        check("     but is rejected as implausible",
              H.lib.m_plaus(ctypes.byref(H.cal(zero))), 0)
        check("all-ones rejected",
              H.lib.m_plaus(ctypes.byref(H.cal([0xFFFF] * 8))), 0)
        for i, nm in enumerate(("c1", "c2", "c3", "c4", "c5", "c6"), start=1):
            one_zero = list(prom)
            one_zero[i] = 0
            check(f"     a single zeroed {nm} rejected",
                  H.lib.m_plaus(ctypes.byref(H.cal(one_zero))), 0)
        check("the real coefficients accepted",
              H.lib.m_plaus(ctypes.byref(cal)), 1)

        print("\nreading plausibility - reject, never clamp")
        check("sea level 1013 mbar at 20 C accepted", H.lib.m_rplaus(2000, 101325), 1)
        check("Istanbul summer, 35 C, 1005 mbar accepted", H.lib.m_rplaus(3500, 100500), 1)
        check("zero pressure rejected", H.lib.m_rplaus(2000, 0), 0)
        check("1300 mbar rejected", H.lib.m_rplaus(2000, 130000), 0)
        check("-50 C rejected", H.lib.m_rplaus(-5000, 101325), 0)
        check("+90 C rejected", H.lib.m_rplaus(9000, 101325), 0)

        print("\nthe int64 that a 32-bit implementation would lose")
        # OFF = C2 * 2^16 alone is ~2.4e9, past INT32_MAX. If any of the
        # intermediate maths were 32-bit this comes out wrong or negative.
        check("OFF exceeds INT32_MAX by construction",
              (DS["c2"] << 16) > 2147483647, True)
        # Recompute rather than reusing p - the second-order sweep above
        # left it holding the last sample of that loop.
        t2, p2 = H.comp(cal, DS["d1"], DS["d2"])
        check("     and pressure is still right", p2, 100009)

    print(f"\n{FAIL} FAILURE(S)\n" if FAIL else "\nALL PASS\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
