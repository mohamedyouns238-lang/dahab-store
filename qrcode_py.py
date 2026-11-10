"""مولّد QR بسيط بلا مكتبات خارجية (وضع البايت، تصحيح أخطاء M، الإصدارات 1–10 = حتى ~213 بايت).

الاستخدام:  matrix(text) -> list[list[int]]  (1 = وحدة سوداء، بدون هامش)
"""

# (عدد كلمات التصحيح لكل كتلة, [(عدد الكتل, كلمات البيانات لكل كتلة), ...]) — مستوى M
_EC = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
       6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
       10: (26, [(4, 43), (1, 44)])}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
          8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
MAX_BYTES = 213

# جداول غالوا GF(256) بالمتعدد 0x11D
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_poly(n):
    p = [1]
    for i in range(n):
        q = [0] * (len(p) + 1)
        for j, c in enumerate(p):
            q[j] ^= c
            q[j + 1] ^= _gmul(c, _EXP[i])
        p = q
    return p


def _rs(data, n):
    gen = _rs_poly(n)
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for i in range(n):
                rem[i] ^= _gmul(gen[i + 1], f)
    return rem


def _capacity(ver):
    n, blocks = _EC[ver]
    return sum(c * d for c, d in blocks)


def _choose_version(nbytes):
    for v in range(1, 11):
        hdr = 4 + (8 if v < 10 else 16)
        if hdr + 8 * nbytes <= 8 * _capacity(v):
            return v
    raise ValueError(f"النص أطول من الحد المسموح للـ QR ({MAX_BYTES} حرفًا)")


def _bits(data, ver):
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))
    put(0b0100, 4)
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    cap = 8 * _capacity(ver)
    bits.extend([0] * min(4, cap - len(bits)))
    bits.extend([0] * (-len(bits) % 8))
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad = 0x11 if pad == 0xEC else 0xEC
    return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def _codewords(data, ver):
    ecn, groups = _EC[ver]
    blocks, pos = [], 0
    for cnt, dl in groups:
        for _ in range(cnt):
            blocks.append(data[pos:pos + dl])
            pos += dl
    ecs = [_rs(b, ecn) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ecn):
        out += [e[i] for e in ecs]
    return out


def _format_bits(mask):
    d = (0b00 << 3) | mask                      # مستوى M = 00
    rem = d << 10
    for i in range(14, 9, -1):
        if rem >> i & 1:
            rem ^= 0x537 << (i - 10)
    return ((d << 10) | rem) ^ 0x5412


def _version_bits(ver):
    rem = ver << 12
    for i in range(17, 11, -1):
        if rem >> i & 1:
            rem ^= 0x1F25 << (i - 12)
    return (ver << 12) | rem


_MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
          lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
          lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
          lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _build(ver, cw, mask):
    n = 17 + 4 * ver
    M = [[0] * n for _ in range(n)]
    F = [[False] * n for _ in range(n)]              # وحدات وظيفية (لا تُقنَّع)

    def setf(r, c, v):
        if 0 <= r < n and 0 <= c < n:
            M[r][c] = 1 if v else 0
            F[r][c] = True
    for (r0, c0) in ((0, 0), (0, n - 7), (n - 7, 0)):        # أنماط التحديد
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)
                setf(r, c, inside and ring)
    for i in range(8, n - 8):                                # التوقيت
        setf(6, i, i % 2 == 0)
        setf(i, 6, i % 2 == 0)
    pos = _ALIGN[ver]
    for r in pos:
        for c in pos:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= n - 9) or (r >= n - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    setf(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    fb = _format_bits(mask)
    for i in range(15):
        bit = fb >> i & 1
        # الجهة الأولى
        if i < 6:
            setf(i, 8, bit)
        elif i < 8:
            setf(i + 1, 8, bit)
        elif i == 8:
            setf(8, 7, bit)
        else:
            setf(8, 14 - i, bit)
        # الجهة الثانية
        if i < 8:
            setf(8, n - 1 - i, bit)
        else:
            setf(n - 15 + i, 8, bit)
    setf(n - 8, 8, 1)                                        # الوحدة الداكنة الثابتة
    if ver >= 7:
        vb = _version_bits(ver)
        for i in range(18):
            bit = vb >> i & 1
            a, b = i // 3, n - 11 + i % 3
            setf(a, b, bit)
            setf(b, a, bit)
    # وضع البيانات بالتعرّج
    bits = [(b >> k) & 1 for b in cw for k in range(7, -1, -1)]
    bi, up = 0, True
    c = n - 1
    while c > 0:
        if c == 6:
            c -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for cc in (c, c - 1):
                if not F[r][cc]:
                    v = bits[bi] if bi < len(bits) else 0
                    bi += 1
                    M[r][cc] = v ^ (1 if _MASKS[mask](r, cc) else 0)
        up = not up
        c -= 2
    return M


def _penalty(M):
    n = len(M)
    p = 0
    for grid in (M, [list(x) for x in zip(*M)]):
        for row in grid:
            run, prev = 1, row[0]
            for v in row[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run, prev = 1, v
            if run >= 5:
                p += run - 2
            s = "".join(map(str, row))
            p += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if M[r][c] == M[r][c + 1] == M[r + 1][c] == M[r + 1][c + 1]:
                p += 3
    dark = sum(map(sum, M))
    p += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return p


def matrix(text):
    data = text.encode("utf-8")
    ver = _choose_version(len(data))
    cw = _codewords(_bits(data, ver), ver)
    best = None
    for m in range(8):
        M = _build(ver, cw, m)
        s = _penalty(M)
        if best is None or s < best[0]:
            best = (s, M)
    return best[1]


def rows(text):
    """نفس المصفوفة كسلاسل نصية من 0/1 (مناسبة للإرسال JSON)."""
    return ["".join(map(str, r)) for r in matrix(text)]


def svg(text, scale=4, border=4, color="#000"):
    M = matrix(text)
    n = len(M) + 2 * border
    path = "".join(f"M{c + border} {r + border}h1v1h-1z" for r, row in enumerate(M) for c, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" height="{n * scale}" '
            f'shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="{color}"/></svg>')
