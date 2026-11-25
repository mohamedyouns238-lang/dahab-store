"""مولّد باركود Code 128 بلا مكتبات خارجية (يقرؤه أي قارئ باركود).
الاستخدام:  svg(text) -> نص SVG   |   modules(text) -> قائمة عرض الأشرطة"""

_P = """212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231
113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211
212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131
113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111
314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211
241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141
214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214
211232 2331112""".split()
START_B, START_C, CODE_C, CODE_B, STOP = 104, 105, 99, 100, 106
MAX_LEN = 40


def _codes(text):
    if not text or len(text) > MAX_LEN or any(not 32 <= ord(ch) <= 126 for ch in text):
        raise ValueError("الباركود يقبل حروفًا إنجليزية وأرقامًا ورموزًا فقط (حتى 40 خانة)")
    if text.isdigit() and len(text) >= 2:
        if len(text) % 2 == 0:
            out = [START_C] + [int(text[i:i + 2]) for i in range(0, len(text), 2)]
        else:
            out = [START_B, ord(text[0]) - 32, CODE_C] + [int(text[i:i + 2]) for i in range(1, len(text), 2)]
    else:
        out = [START_B] + [ord(ch) - 32 for ch in text]
    chk = (out[0] + sum(i * v for i, v in enumerate(out[1:], 1))) % 103
    return out + [chk, STOP]


def modules(text):
    """قائمة أطوال متتالية (أسود، أبيض، أسود، ...) بوحدات المقاس."""
    w = []
    for c in _codes(text):
        w += [int(d) for d in _P[c]]
    return w


def svg(text, height=40, quiet=10, color="#000"):
    w = modules(text)
    total = sum(w) + 2 * quiet
    x, bars, black = quiet, [], True
    for n in w:
        if black:
            bars.append(f"M{x} 0h{n}v{height}h-{n}z")
        x += n
        black = not black
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {height}" preserveAspectRatio="none" '
            f'shape-rendering="crispEdges"><path d="{"".join(bars)}" fill="{color}"/></svg>')
