"""يولّد أيقونة التطبيق (أسود × ذهبي):  python make_icon.py"""
import math
from PIL import Image, ImageDraw
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((0, 0, S - 1, S - 1), radius=210, fill=(11, 11, 11, 255))
GOLD, GOLD_L, GOLD_D = (212, 175, 55), (241, 215, 122), (168, 128, 28)
d.rounded_rectangle((34, 34, S - 35, S - 35), radius=180, outline=GOLD_D, width=10)
def star(cx, cy, r1, r2, n=8, rot=0):
    pts = []
    for i in range(n * 2):
        r = r1 if i % 2 == 0 else r2
        a = rot + math.pi * i / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts
c = S // 2
d.polygon(star(c, c, 440, 300, 8, -math.pi / 2), outline=GOLD, width=14)
d.polygon(star(c, c, 360, 250, 8, -math.pi / 2 + math.pi / 8), outline=GOLD_D, width=8)
w = 70
d.rectangle((c - 130, c - 190, c - 130 + w, c + 190), fill=GOLD_L)
d.pieslice((c - 220, c - 190, c + 170, c + 190), -90, 90, fill=GOLD_L)
d.pieslice((c - 220 + w + 40, c - 190 + w, c + 170 - w, c + 190 - w), -90, 90, fill=(11, 11, 11, 255))
d.rectangle((c - 130 + w, c - 190 + w, c - 130 + w + 60, c + 190 - w), fill=(11, 11, 11, 255))
im.resize((256, 256), Image.LANCZOS).save("static/icon.png")
im.save("assets/dahab.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("icons written")
