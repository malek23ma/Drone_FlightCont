/* MS5611 pure arithmetic - see ms5611_calc.h for why this is its own file.
 * Host-tested by tools/ms5611_test.py. */

#include "ms5611_calc.h"

uint8_t ms5611_prom_crc4(const uint16_t prom[MS5611_PROM_WORDS])
{
    /* TE application note AN520. The CRC is computed over the whole 8-word
     * PROM with the CRC nibble itself zeroed, so work on a local copy - the
     * caller's image must come back untouched, since it is the thing being
     * validated. */
    uint16_t p[MS5611_PROM_WORDS];
    for (int i = 0; i < MS5611_PROM_WORDS; i++) p[i] = prom[i];
    p[7] = (uint16_t)(p[7] & 0xFF00u);   /* zero the stored CRC nibble */

    uint16_t rem = 0;
    for (int i = 0; i < 16; i++) {
        /* Feed the block a BYTE at a time, high byte first. */
        if (i & 1) rem ^= (uint16_t)(p[i >> 1] & 0x00FFu);
        else       rem ^= (uint16_t)(p[i >> 1] >> 8);

        for (int bit = 8; bit > 0; bit--) {
            if (rem & 0x8000u) rem = (uint16_t)((rem << 1) ^ 0x3000u);
            else               rem = (uint16_t)(rem << 1);
        }
    }
    return (uint8_t)((rem >> 12) & 0x000Fu);
}

int ms5611_prom_crc_ok(const uint16_t prom[MS5611_PROM_WORDS])
{
    return ms5611_prom_crc4(prom) == (uint8_t)(prom[7] & 0x000Fu);
}

void ms5611_cal_from_prom(ms5611_cal_t *cal,
                          const uint16_t prom[MS5611_PROM_WORDS])
{
    for (int i = 0; i < MS5611_PROM_WORDS; i++) cal->prom[i] = prom[i];
    cal->c1 = prom[1];
    cal->c2 = prom[2];
    cal->c3 = prom[3];
    cal->c4 = prom[4];
    cal->c5 = prom[5];
    cal->c6 = prom[6];
}

int ms5611_cal_plausible(const ms5611_cal_t *cal)
{
    /* An all-zero PROM has a valid CRC of 0, so the CRC alone does not
     * catch a device that is answering but not calibrated - which is
     * exactly the shape of the DPS310 failure. Every coefficient of a real
     * part is a substantial non-zero number; C5 in particular is the
     * reference temperature and is always around 30000. */
    if (cal->c1 == 0 || cal->c2 == 0 || cal->c3 == 0 ||
        cal->c4 == 0 || cal->c5 == 0 || cal->c6 == 0) return 0;
    if (cal->c1 == 0xFFFFu && cal->c2 == 0xFFFFu && cal->c3 == 0xFFFFu &&
        cal->c4 == 0xFFFFu && cal->c5 == 0xFFFFu && cal->c6 == 0xFFFFu)
        return 0;
    return 1;
}

void ms5611_compensate(const ms5611_cal_t *cal, uint32_t d1, uint32_t d2,
                       int32_t *temp_c100, int32_t *press_pa)
{
    /* First order, straight out of the datasheet. int64 throughout: OFF and
     * SENS both exceed 32 bits by construction (C2 * 2^16 is already up to
     * 4.3e9), and doing this in 32-bit is the classic way to get a
     * plausible-looking pressure that is completely wrong. */
    const int64_t dT = (int64_t)d2 - ((int64_t)cal->c5 << 8);
    int64_t temp = 2000 + ((dT * (int64_t)cal->c6) >> 23);

    int64_t off  = ((int64_t)cal->c2 << 16) + (((int64_t)cal->c4 * dT) >> 7);
    int64_t sens = ((int64_t)cal->c1 << 15) + (((int64_t)cal->c3 * dT) >> 8);

    /* Second order, below 20 C. The aircraft flies in Istanbul winters and
     * the correction is tens of pascals - small, but it is free and the
     * datasheet's own example does not exercise it, so it is the half most
     * likely to be typed in wrong and never noticed. */
    if (temp < 2000) {
        const int64_t t_off = temp - 2000;
        int64_t t2    = (dT * dT) >> 31;
        int64_t off2  = (5 * t_off * t_off) >> 1;
        int64_t sens2 = (5 * t_off * t_off) >> 2;

        if (temp < -1500) {
            const int64_t t_frz = temp + 1500;
            off2  += 7 * t_frz * t_frz;
            sens2 += (11 * t_frz * t_frz) >> 1;
        }
        temp -= t2;
        off  -= off2;
        sens -= sens2;
    }

    const int64_t p = ((((int64_t)d1 * sens) >> 21) - off) >> 15;

    *temp_c100 = (int32_t)temp;
    *press_pa  = (int32_t)p;
}

int ms5611_reading_plausible(int32_t temp_c100, int32_t press_pa)
{
    /* Datasheet operating range, with no slack added. Reject, never clamp. */
    if (press_pa < 1000 || press_pa > 120000) return 0;   /* 10..1200 mbar */
    if (temp_c100 < -4000 || temp_c100 > 8500) return 0;  /* -40..+85 C    */
    return 1;
}
