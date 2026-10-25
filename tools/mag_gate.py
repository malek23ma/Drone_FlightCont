#!/usr/bin/env python3
"""Why is the magnetometer gated out, and what does it depend on?

    python tools/mag_gate.py aug27_s*.csv

THE QUESTION. The MMC5603 answers reliably but its value is rejected by the
magnitude gate in roughly 13 % of frames, and while it is rejected Madgwick
runs 6-DOF with no heading reference - which is where the -1.4 deg/s yaw
drift comes from. the author wants the CAUSE found before the part is replaced.

WHAT THE GATE IS (fusion.c:861): accept only 10 uT < |B| < 120 uT.
Two facts make the lower bound the one to watch:
  * the MMC5603 READS ABOUT HALF SCALE, so Istanbul's ~48 uT field
    arrives as ~24 uT - a factor of 2.4 above the floor, not 4.8
  * the hard-iron calibration has never been done, so the measured sphere
    is OFFSET from the origin. An offset sphere's magnitude changes with
    HEADING: it is largest pointing one way and smallest pointing the
    opposite way. If the offset is comparable to the field, the small side
    dips under 10 uT and the gate fires - on heading, not on anything
    electrical.

WHAT THIS SCRIPT DOES. The blackbox does not log raw mag values, but it
logs the two health bits, throttle and attitude every frame at 250 Hz.
Binning the gate rate against each of those separates the candidates:

  gating tracks THROTTLE   -> motor current / EMI. Wiring or placement.
  gating tracks YAW        -> hard-iron offset. Do the six-face tumble.
  gating tracks ROLL/PITCH -> hard iron with a strong vertical component.
  gating tracks TIME only  -> thermal, or the chip.
  gating is flat everywhere -> the gate threshold is simply too tight for
                               a half-scale part, not a hardware fault.

Bit 2 = SENSOR_MAG_OK, "the chip replied". Bit 3 = SENSOR_MAG_GATED, "the
reply was believable". Frames where the chip did NOT reply are excluded
throughout: a chip that did not answer was not gated, and mixing the two
is how "the mag is bad" becomes unfalsifiable.
"""

import csv
import glob
import math
import sys

MAG_OK = 1 << 2
MAG_GATED = 1 << 3


def bar(frac, width=28):
    n = 0 if frac != frac else int(round(frac * width))
    return "#" * n + "." * (width - n)


def binned(rows, key, edges, label, fmt="{:g}"):
    """Gate rate within each bin of `key`. Returns the per-bin rates."""
    print(f"\n  gate rate by {label}")
    print(f"  {'bin':>16}  {'frames':>7}  {'gated':>7}   0%{' ' * 22}100%")
    out = []
    for lo, hi in zip(edges, edges[1:]):
        sel = [r for r in rows if lo <= key(r) < hi]
        if len(sel) < 50:
            continue
        g = sum(1 for r in sel if not (int(r["sensor_health"]) & MAG_GATED))
        rate = g / len(sel)
        out.append((lo, hi, len(sel), rate))
        print(f"  {fmt.format(lo):>7}-{fmt.format(hi):<8}  {len(sel):7d}  "
              f"{rate*100:6.1f}%  {bar(rate)}")
    return out


def spread(binlist):
    """max - min gate rate across bins, the strength of the dependence."""
    if len(binlist) < 2:
        return 0.0
    rates = [b[3] for b in binlist]
    return max(rates) - min(rates)


def main(argv):
    paths = []
    for a in argv[1:]:
        paths.extend(sorted(glob.glob(a)))
    if not paths:
        print(__doc__)
        return 1

    rows = []
    for p in paths:
        with open(p, newline="") as fh:
            rows.extend(list(csv.DictReader(fh)))
    if not rows:
        print("no rows")
        return 1

    total = len(rows)
    replied = [r for r in rows if int(r["sensor_health"]) & MAG_OK]
    if not replied:
        print(f"{total} frames, and the mag NEVER replied in any of them "
              f"(bit 2 always clear). That is a dead or absent chip, not a "
              f"gating question.")
        return 0

    gated = [r for r in replied if not (int(r["sensor_health"]) & MAG_GATED)]

    print(f"\n=== {len(paths)} file(s), {total} frames ===")
    print(f"  chip replied      : {len(replied)} ({100*len(replied)/total:.1f} %)")
    print(f"  of those, GATED   : {len(gated)} "
          f"({100*len(gated)/len(replied):.1f} %)")
    if not gated:
        print("\n  Never gated in this data - nothing to explain here.")
        return 0

    # Throttle: the motor-current / EMI hypothesis.
    b_thr = binned(replied, lambda r: int(r["throttle"]),
                   [1000, 1100, 1200, 1300, 1400, 1500, 1600, 2100],
                   "THROTTLE (us)  - tests motor current / EMI", "{:d}")

    # Yaw: the hard-iron hypothesis. An offset sphere's magnitude is a
    # sinusoid in heading, so a hard-iron problem shows as a smooth
    # one-cycle-per-revolution pattern, not as noise.
    b_yaw = binned(replied, lambda r: float(r["yaw"]) % 360.0,
                   list(range(0, 361, 30)),
                   "YAW (deg)      - tests hard-iron offset", "{:.0f}")

    # Roll and pitch: hard iron with a vertical component, or the tilt
    # changing which part of the offset sphere is sampled.
    b_rol = binned(replied, lambda r: abs(float(r["roll"])),
                   [0, 5, 10, 15, 20, 30, 90],
                   "|ROLL| (deg)", "{:.0f}")
    b_pit = binned(replied, lambda r: abs(float(r["pitch"])),
                   [0, 5, 10, 15, 20, 30, 90],
                   "|PITCH| (deg)", "{:.0f}")

    # Time: thermal drift, or something that simply gets worse.
    t0 = int(replied[0]["t_us"])
    b_t = binned(replied, lambda r: (int(r["t_us"]) - t0) / 1e6,
                 [0, 10, 20, 30, 40, 60, 90, 1e9],
                 "TIME since first frame (s)", "{:.0f}")

    # ---- Shape matters as much as spread -------------------------------
    # A dependence on motor CURRENT must be monotonic in throttle: current
    # only goes up. A rate that peaks in the middle and falls again at full
    # throttle is not current, whatever the spread says.
    def monotonic_frac(binlist):
        """How much of the variation is a consistent climb? 1.0 = perfectly
        increasing, 0.0 = the ends are equal and it is all a bump."""
        if len(binlist) < 3:
            return float("nan")
        rates = [b[3] for b in binlist]
        rng = max(rates) - min(rates)
        return abs(rates[-1] - rates[0]) / rng if rng > 0 else 0.0

    print("\n  === how strongly does each one explain it? ===")
    print("  spread = max-min across bins.  end-to-end = how much of that")
    print("  is a consistent trend rather than a bump in the middle.")
    ranked = sorted(
        [("throttle", spread(b_thr), monotonic_frac(b_thr)),
         ("yaw", spread(b_yaw), monotonic_frac(b_yaw)),
         ("|roll|", spread(b_rol), monotonic_frac(b_rol)),
         ("|pitch|", spread(b_pit), monotonic_frac(b_pit)),
         ("time", spread(b_t), monotonic_frac(b_t))],
        key=lambda x: -x[1])
    for name, sp, mono in ranked:
        note = ""
        if mono == mono and mono < 0.35 and sp > 0.10:
            note = "  <- PEAKS IN THE MIDDLE, not a trend"
        print(f"    {name:>9}: spread {sp*100:5.1f} pts   "
              f"end-to-end {mono*100:5.0f} %{note}")

    # ---- Deconfound: throttle and heading move together in a flight -----
    # The aircraft flies particular headings at particular throttles, so a
    # one-variable table cannot tell which is driving. Cross-tabulate.
    print("\n  === cross-tab: gate rate by THROTTLE within each YAW band ===")
    print("  If the throttle pattern survives inside a fixed heading, it is")
    print("  real. If it collapses, throttle was standing in for heading.")
    yaw_bands = [(240, 300), (300, 330), (330, 360)]
    thr_bands = [(1000, 1200), (1200, 1400), (1400, 1600)]
    hdr = "  {:>14}".format("yaw \\ throttle")
    for tl, th in thr_bands:
        hdr += f"  {tl}-{th:<5}"
    print(hdr)
    for yl, yh in yaw_bands:
        line = f"  {yl:>6}-{yh:<7}"
        for tl, th in thr_bands:
            sel = [r for r in replied
                   if yl <= float(r["yaw"]) % 360.0 < yh
                   and tl <= int(r["throttle"]) < th]
            if len(sel) < 50:
                line += "        --  "
            else:
                g = sum(1 for r in sel
                        if not (int(r["sensor_health"]) & MAG_GATED))
                line += f"  {100*g/len(sel):6.1f}% "
        print(line)

    print("\n  === and the other way: by YAW within each THROTTLE band ===")
    hdr = "  {:>14}".format("throttle \\ yaw")
    for yl, yh in yaw_bands:
        hdr += f"    {yl}-{yh:<4}"
    print(hdr)
    for tl, th in thr_bands:
        line = f"  {tl:>6}-{th:<7}"
        for yl, yh in yaw_bands:
            sel = [r for r in replied
                   if yl <= float(r["yaw"]) % 360.0 < yh
                   and tl <= int(r["throttle"]) < th]
            if len(sel) < 50:
                line += "        --  "
            else:
                g = sum(1 for r in sel
                        if not (int(r["sensor_health"]) & MAG_GATED))
                line += f"  {100*g/len(sel):6.1f}% "
        print(line)

    top, topsp, topmono = ranked[0]
    print()
    if topsp < 0.10:
        print("  READING: the gate rate barely moves with any of these. Not")
        print("  the signature of motor noise or of hard iron - both would")
        print("  track something. A flat rate on a part that reads HALF")
        print("  SCALE points at the gate itself: 10 uT is only ~2.4x below")
        print("  a half-scale Istanbul field. Six-face tumble first.")
    elif top == "throttle" and topmono < 0.35:
        print("  READING, in two parts.")
        print()
        print("  1. It is NOT motor-current EMI. Throttle is the strongest")
        print("     single dependence, but the rate PEAKS IN THE MIDDLE and")
        print("     is near its lowest at full throttle. Current only rises,")
        print("     so an effect driven by current must rise with it. A")
        print("     mid-range peak is a resonance at a particular RPM, or")
        print("     something throttle merely travels with. Either way,")
        print("     moving the mag away from the ESC would not fix this, and")
        print("     neither would a new part.")
        print()
        print("  2. There is ALSO a heading floor - read the cross-tab. If")
        print("     one yaw band stays clean at every throttle while others")
        print("     do not, the magnitude is sitting close to the 10 uT gate")
        print("     on some headings and comfortably above it on others.")
        print("     That is what an uncalibrated HARD IRON offset does: it")
        print("     shifts the sphere off the origin, so |B| becomes a")
        print("     function of heading. On the near side, ordinary noise")
        print("     crosses the floor; on the far side nothing does.")
        print()
        print("  THE CONFOUND, and it is a real one: `yaw` here is the FUSED")
        print("  yaw, and Madgwick drifts about -1.4 deg/s precisely BECAUSE")
        print("  the mag is gated. Over a 50 s flight that is ~70 deg of")
        print("  drift against a ~120 deg observed spread. So gating moves")
        print("  the heading as well as the other way round, and the yaw")
        print("  axis cannot carry the weight of a conclusion on its own.")
        print("  The throttle finding does not have this problem - throttle")
        print("  is a stick position, uncontaminated by the estimator.")
        print()
        print("  WHAT TO DO, cheapest first:")
        print("    a) the six-face tumble. Free, already owed, and it fixes")
        print("       hard iron whether or not hard iron is the whole story.")
        print("       MAGSET,0,0,0 -> MAGCAP,1 -> capture.py -> mag_cal.py.")
        print("    b) stream `D,` lines and look at the actual |B|. This")
        print("       script can only see WHEN the gate fired, never by how")
        print("       much it missed - and the margin is the whole question.")
        print("    c) only then consider the gate bounds. 10 uT under a")
        print("       HALF-SCALE part is a tighter floor than it looks.")
        print("  Do NOT buy a new magnetometer on this evidence.")
    elif top == "throttle":
        print("  READING: gating rises with THROTTLE monotonically -> motor")
        print("  current. Look at where the mag sits relative to the ESC and")
        print("  the battery leads. Replacing the part would change nothing.")
    elif top == "yaw":
        print("  READING: gating tracks YAW -> a hard-iron offset, the")
        print("  calibration never yet done. Six-face tumble.")
        print("  CAVEAT: `yaw` is the FUSED yaw and it drifts BECAUSE the")
        print("  mag is gated, so some of this correlation runs backwards.")
        print("  Confirm with raw |B| from a `D,` stream before acting.")
    elif top in ("|roll|", "|pitch|"):
        print(f"  READING: gating tracks {top} -> hard iron with a vertical")
        print("  component, or the sensor seeing the pack differently as it")
        print("  tilts. Six-face tumble first; if it survives that, move it.")
    else:
        print("  READING: gating grows with TIME and not with attitude or")
        print("  throttle -> thermal, or degradation within a run.")

    print("\n  Caveat worth stating: the blackbox does NOT log raw mag")
    print("  values, so this shows WHEN the gate fires, never by how much")
    print("  the magnitude missed. To get that, stream `D,` lines with")
    print("  tools/capture.py - mag_x/y/z are in there.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
