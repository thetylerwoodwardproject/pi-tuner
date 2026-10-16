"""Synthesise SAME AFSK bursts for tests."""
import math, random, struct
def afsk(msg, rate, noise=0.0, amp=8000, seed=1):
    rnd = random.Random(seed)
    bits = [1,1,0,1,0,1,0,1]*16
    for ch in msg.encode():
        bits += [(ch >> i) & 1 for i in range(8)]
    out, ph = [], 0.0
    spb = rate / 520.83
    n = int(len(bits) * spb)
    for i in range(n):
        f = 2083.33 if bits[min(int(i / spb), len(bits)-1)] else 1562.5
        ph += 2*math.pi*f/rate
        out.append(int(amp*math.sin(ph) + rnd.gauss(0, noise)))
    return out
def burst_stream(msg, rate, noise=0.0, lead=0.6, **kw):
    sil = lambda s: [int(random.gauss(0, noise)) for _ in range(int(rate*s))]
    pcm = sil(lead)
    for _ in range(3):
        pcm += afsk(msg, rate, noise, **kw) + sil(1.0)
    return pcm
