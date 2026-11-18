"""Generate the Damson sign-in scene as one inline SVG (seeded, reproducible).

Story, top to bottom: a calm sky (headline space, upper left), lavender hills, then a small town
on a rise: the laboratory in the middle, its clients around it (a clinic, a water utility, a
factory, an office row) along one road. The laboratory's work is under way: a courier van with
the flask on its side drives to the laboratory, cartons of supplies are stacked by its door and a
sample cooler waits on the clinic's step for pickup. A meadow and a path lead up to the
laboratory's door. Every colour is a class whose fill comes from --sc-*
custom properties, so the same SVG renders morning (light) and evening with lit windows (dark).
viewBox 1100 x 1000, sliced xMidYMax, so narrow panels crop the sides and wide panels crop sky,
never the town.
"""

import math
import random

W, H = 1100, 1000
GROUND = 760  # the town's street line
R = random.Random(20261117)


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def smooth(points):
    """Catmull-Rom through points -> cubic Bezier path data."""
    d = f"M{f(points[0][0])} {f(points[0][1])}"
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f"C{f(c1[0])} {f(c1[1])} {f(c2[0])} {f(c2[1])} {f(p2[0])} {f(p2[1])}"
    return d


def wave(base, amps, step=24, x0=-40, x1=W + 40):
    phases = [R.uniform(0, math.tau) for _ in amps]
    pts = []
    x = x0
    while x <= x1:
        y = base + sum(a * math.sin(x / p + ph) for (a, p), ph in zip(amps, phases, strict=True))
        pts.append((x, y))
        x += step
    return pts


def band(top, bottom=H + 20):
    return smooth(top) + f"L{W + 40} {bottom}L-40 {bottom}Z"


out = []
add = out.append

add(
    "<defs>"
    '<linearGradient id="sc-sky" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" class="sc-s-sky-top"/><stop offset="0.55" class="sc-s-sky-mid"/>'
    '<stop offset="1" class="sc-s-sky-low"/></linearGradient>'
    '<radialGradient id="sc-glow" cx="0.78" cy="0.52" r="0.45">'
    '<stop offset="0" class="sc-s-glow"/><stop offset="1" class="sc-s-glow" stop-opacity="0"/>'
    "</radialGradient>"
    '<radialGradient id="sc-moon" cx="0.5" cy="0.5" r="0.5">'
    '<stop offset="0" class="sc-s-moon"/><stop offset="1" class="sc-s-moon" stop-opacity="0"/>'
    "</radialGradient>"
    '<linearGradient id="sc-haze" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" class="sc-s-haze" stop-opacity="0"/><stop offset="0.5" class="sc-s-haze" '
    'stop-opacity="0.75"/><stop offset="1" class="sc-s-haze" stop-opacity="0"/></linearGradient>'
    '<filter id="sc-soft" x="-20%" y="-50%" width="140%" height="200%">'
    '<feGaussianBlur stdDeviation="6"/></filter>'
    '<filter id="sc-grain" x="0" y="0" width="100%" height="100%">'
    '<feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="7"/>'
    '<feColorMatrix values="0 0 0 0 0.5 0 0 0 0 0.5 0 0 0 0 0.5 0 0 0 0.9 0"/></filter>'
    "</defs>"
)

# ---------------------------------------------- sky, sun glow, night stars and moon
add(f'<rect width="{W}" height="{H}" fill="url(#sc-sky)"/>')
add(f'<rect class="sc-day" width="{W}" height="{H}" fill="url(#sc-glow)"/>')
add('<circle class="sc-day sc-sun" cx="860" cy="430" r="46"/>')
stars = []
for _ in range(80):
    x, y = R.uniform(560, 1100), R.uniform(20, 470)
    stars.append(
        f'<circle cx="{f(x)}" cy="{f(y)}" r="{f(R.choice([0.6, 0.8, 0.8, 1.1, 1.4]))}" '
        f'opacity="{f(R.uniform(0.35, 0.9))}"/>'
    )
add(
    '<g class="sc-night"><g class="sc-star">' + "".join(stars) + "</g>"
    '<circle cx="880" cy="150" r="58" fill="url(#sc-moon)" opacity="0.5"/>'
    '<circle class="sc-moonface" cx="880" cy="150" r="14"/></g>'
)

# clouds: soft, right half only (the upper left stays calm for the headline)
clouds = []
for cx, cy, s in ((820, 250, 1.0), (1000, 330, 0.8), (690, 370, 0.65), (960, 160, 0.55)):
    for _ in range(6):
        clouds.append(
            f'<ellipse cx="{f(cx + R.uniform(-90, 90) * s)}" cy="{f(cy + R.uniform(-10, 8) * s)}" '
            f'rx="{f(R.uniform(50, 110) * s)}" ry="{f(R.uniform(9, 18) * s)}"/>'
        )
add('<g class="sc-cloud" filter="url(#sc-soft)">' + "".join(clouds) + "</g>")

# ---------------------------------------------- hills (atmospheric perspective)
add(f'<path class="sc-ridge1" d="{band(wave(560, [(16, 130), (7, 47)]))}"/>')
add(f'<path class="sc-ridge2" d="{band(wave(600, [(14, 160), (6, 55)]))}"/>')
add(f'<rect x="0" y="520" width="{W}" height="140" fill="url(#sc-haze)"/>')
add(f'<path class="sc-hill1" d="{band(wave(655, [(18, 170), (8, 60)]))}"/>')
far_trees = []
for x in range(-20, W + 20, 26):
    y = 650 + 10 * math.sin(x / 90) + R.uniform(-4, 4)
    far_trees.append(
        f'<circle cx="{f(x + R.uniform(-6, 6))}" cy="{f(y)}" r="{f(R.uniform(9, 15))}"/>'
    )
add('<g class="sc-trees1">' + "".join(far_trees) + "</g>")

# the rise the town stands on
rise = [(-40, 742), (160, 732), (380, 722), (560, 716), (760, 722), (960, 734), (1140, 744)]
add(f'<path class="sc-hill2" d="{band(rise)}"/>')


# ---------------------------------------------- buildings
def windows(x0, y0, cols, rows, w=14, h=16, gx=10, gy=12, lit=0.45):
    cells = []
    for r in range(rows):
        for c in range(cols):
            cls = "sc-window-lit" if R.random() < lit else "sc-window"
            cells.append(
                f'<rect class="{cls}" x="{f(x0 + c * (w + gx))}" y="{f(y0 + r * (h + gy))}" '
                f'width="{w}" height="{h}" rx="2"/>'
            )
    return "".join(cells)


town = []
# office row, far left and far right (backdrop)
for x, w, h in ((30, 70, 120), (96, 54, 92), (930, 60, 110), (986, 80, 136), (1060, 60, 96)):
    town.append(
        f'<rect class="sc-bldg-far" x="{x}" y="{GROUND - h}" width="{w}" height="{h}" rx="3"/>'
    )
    town.append(
        windows(
            x + 10,
            GROUND - h + 14,
            max(1, (w - 14) // 22),
            max(1, (h - 30) // 30),
            w=10,
            h=12,
            gx=12,
            gy=18,
            lit=0.35,
        )
    )

# clinic (left): gabled house with a porch
cx0, cw, ch = 150, 170, 96
town.append(
    f'<rect class="sc-bldg" x="{cx0}" y="{GROUND - ch}" width="{cw}" height="{ch}" rx="3"/>'
)
town.append(
    f'<path class="sc-roof" d="M{cx0 - 12} {GROUND - ch + 4}L{cx0 + cw / 2} {GROUND - ch - 46}'
    f'L{cx0 + cw + 12} {GROUND - ch + 4}Z"/>'
)
town.append(windows(cx0 + 18, GROUND - ch + 22, 2, 2, w=22, h=18, gx=14, gy=14, lit=0.5))
town.append(windows(cx0 + 112, GROUND - ch + 22, 1, 2, w=22, h=18, gy=14, lit=0.5))
town.append(
    f'<rect class="sc-door" x="{cx0 + 72}" y="{GROUND - 44}" width="26" height="44" rx="3"/>'
)
town.append(
    f'<rect class="sc-awning" x="{cx0 + 62}" y="{GROUND - 52}" width="46" height="9" rx="3"/>'
)

# water utility (left of centre): a tank on legs
tx, ty = 375, GROUND - 190
town.append(
    f'<path class="sc-frame" fill="none" stroke-width="5" stroke-linecap="round" '
    f'd="M{tx - 28} {GROUND}L{tx - 18} {ty + 60}M{tx + 28} {GROUND}L{tx + 18} {ty + 60}'
    f"M{tx - 25} {GROUND - 40}L{tx + 25} {GROUND - 80}"
    f'M{tx + 25} {GROUND - 40}L{tx - 25} {GROUND - 80}"/>'
)
town.append(f'<rect class="sc-bldg" x="{tx - 44}" y="{ty}" width="88" height="62" rx="18"/>')
town.append(f'<path class="sc-roof" d="M{tx - 46} {ty + 10}Q{tx} {ty - 34} {tx + 46} {ty + 10}Z"/>')
town.append(f'<rect class="sc-band" x="{tx - 44}" y="{ty + 30}" width="88" height="8"/>')

# the laboratory (centre): a main block, a taller wing and the flask emblem
lx, lw, lh = 470, 250, 150
town.append(f'<rect class="sc-lab" x="{lx}" y="{GROUND - lh}" width="{lw}" height="{lh}" rx="4"/>')
town.append(
    f'<rect class="sc-lab-wing" x="{lx + 150}" y="{GROUND - lh - 70}" width="100" '
    f'height="{lh + 70}" rx="4"/>'
)
town.append(
    f'<rect class="sc-lab-trim" x="{lx - 6}" y="{GROUND - lh - 8}" width="{lw - 94}" '
    'height="10" rx="3"/>'
)
town.append(
    f'<rect class="sc-lab-trim" x="{lx + 144}" y="{GROUND - lh - 78}" width="112" '
    'height="10" rx="3"/>'
)
town.append(windows(lx + 16, GROUND - lh + 20, 5, 3, w=16, h=18, gx=9, gy=14, lit=0.55))
town.append(windows(lx + 166, GROUND - lh - 50, 3, 6, w=14, h=16, gx=9, gy=14, lit=0.6))
town.append(
    f'<rect class="sc-door" x="{lx + 60}" y="{GROUND - 46}" width="40" height="46" rx="3"/>'
)
ex, ey = lx + 200, GROUND - lh - 118
town.append(f'<circle class="sc-emblem" cx="{ex}" cy="{ey}" r="30"/>')
town.append(
    f'<path class="sc-flask" fill="none" stroke-width="4.5" stroke-linecap="round" '
    f'stroke-linejoin="round" d="M{ex - 8} {ey - 16}h16M{ex - 5} {ey - 16}v10l-11 17a3 3 0 0 0 '
    f'2.6 4.5h26.8a3 3 0 0 0 2.6-4.5L{ex + 5} {ey - 6}v-10"/>'
)
town.append(
    f'<path class="sc-flask-fill" d="M{ex - 11} {ey + 7}h22l4 6.5a3 3 0 0 1-2.6 4.5h-24.8'
    'a3 3 0 0 1-2.6-4.5z"/>'
)
town.append(f'<rect class="sc-mast" x="{ex - 2}" y="{ey + 30}" width="4" height="12"/>')

# factory (right): sawtooth roof and a chimney
fx, fw, fh = 790, 150, 84
town.append(
    f'<rect class="sc-chimney" x="{fx + 116}" y="{GROUND - fh - 90}" width="18" '
    'height="96" rx="2"/>'
)
town.append(f'<rect class="sc-bldg" x="{fx}" y="{GROUND - fh}" width="{fw}" height="{fh}" rx="3"/>')
teeth = "".join(
    f"L{fx + i * 50 + 34} {GROUND - fh - 30}L{fx + i * 50 + 50} {GROUND - fh}" for i in range(3)
)
town.append(f'<path class="sc-roof" d="M{fx} {GROUND - fh}{teeth}Z"/>')
town.append(windows(fx + 14, GROUND - fh + 20, 5, 2, w=16, h=14, gx=11, gy=14, lit=0.45))
puffs = "".join(
    f'<circle cx="{f(fx + 125 + i * 16)}" cy="{f(GROUND - fh - 104 - i * 18)}" r="{9 + i * 4}"/>'
    for i in range(3)
)
town.append(f'<g class="sc-steam" filter="url(#sc-soft)">{puffs}</g>')

add("".join(town))

# ---------------------------------------------- the road, meadow, path to the laboratory's door
ROAD = 26  # the road's depth below the street line
add(f'<rect class="sc-street" x="-40" y="{GROUND}" width="{W + 80}" height="{ROAD}"/>')
add(
    '<path class="sc-road-line" fill="none" stroke-width="2.5" stroke-dasharray="18 14" '
    f'd="M-40 {GROUND + 13}H{W + 40}"/>'
)
meadow = wave(GROUND + 38, [(8, 140), (3, 40)], step=20)
add(f'<path class="sc-meadow-far" d="{band(meadow)}"/>')
near = wave(GROUND + 120, [(14, 180), (5, 50)], step=20)
add(f'<path class="sc-meadow-near" d="{band(near)}"/>')
door = lx + 80
add(
    f'<path class="sc-path" d="M{door - 16} {GROUND + ROAD}C{door - 30} {GROUND + 90} {door - 140} '
    f"{GROUND + 150} {door - 190} {H + 20}L{door + 40} {H + 20}C{door + 10} {GROUND + 150} "
    f'{door + 20} {GROUND + 80} {door + 16} {GROUND + ROAD}Z"/>'
)


# ---------------------------------------------- operations: supplies and a courier on the road
def carton(x, y, w, h):
    """A kraft carton with a taped seam."""
    return (
        f'<rect class="sc-box" x="{f(x)}" y="{f(y)}" width="{w}" height="{h}" rx="2"/>'
        f'<rect class="sc-box-tape" x="{f(x + w / 2 - 2.5)}" y="{f(y)}" width="5" height="{h}"/>'
    )


# supplies stacked by the laboratory's door; a sample cooler on the clinic's step for pickup
ops = [carton(lx + 114, GROUND - 22, 26, 22), carton(lx + 142, GROUND - 22, 26, 22)]
ops.append(carton(lx + 127, GROUND - 42, 26, 20))
ops.append(
    f'<rect class="sc-box" x="{cx0 + 40}" y="{GROUND - 18}" width="24" height="18" rx="2"/>'
    f'<rect class="sc-awning" x="{cx0 + 38}" y="{GROUND - 21}" width="28" height="6" rx="2"/>'
)

# courier vans, drawn once at the origin (rear of the van, axle line) and placed by transform: both
# bring pickups in to the laboratory, one from the clinic's side, one from the factory's
VAN = (
    '<rect class="sc-van" y="-46" width="66" height="46" rx="5"/>'
    '<path class="sc-van" d="M62 -34h17q6 0 9.5 5.4l8.5 12.6v14.5a2 2 0 0 1-2 2H62z"/>'
    '<path class="sc-window" d="M70 -29h8.5l7 10.5h-15.5z"/>'
    '<rect class="sc-window-lit" x="92" y="-12" width="5" height="5" rx="1.5"/>'
    '<rect class="sc-awning" y="-13" width="66" height="5"/>'
    '<circle class="sc-emblem" cx="31" cy="-28" r="12"/>'
    '<path class="sc-flask" fill="none" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round" d="M27.5 -34.5h7M29 -34.5v4l-4.6 7a1.3 1.3 0 0 0 1.1 2h11a1.3 1.3 0 0 '
    '0 1.1-2L33 -30.5v-4"/>'
    '<circle class="sc-tyre" cx="18" r="8"/><circle class="sc-hub" cx="18" r="3"/>'
    '<circle class="sc-tyre" cx="80" r="8"/><circle class="sc-hub" cx="80" r="3"/>'
)
ops.append(f'<g transform="translate(330 {GROUND + 17}) scale(1.3)">{VAN}</g>')
ops.append(f'<g transform="translate(800 {GROUND + 15}) scale(-1.05 1.05)">{VAN}</g>')
add("".join(ops))
# round trees along the meadow, kept off the path
trees = []
for x, y, s in (
    (60, 800, 1.1),
    (250, 812, 0.9),
    (330, 870, 1.25),
    (760, 820, 1.0),
    (880, 860, 1.3),
    (1040, 820, 1.05),
    (150, 905, 1.4),
):
    trees.append(
        f'<rect class="sc-trunk" x="{f(x - 3 * s)}" y="{f(y)}" width="{f(6 * s)}" '
        f'height="{f(30 * s)}" rx="2"/>'
    )
    for dx, dy, r in ((0, -10, 24), (-14, 2, 18), (14, 2, 18), (0, 8, 16)):
        trees.append(
            f'<circle class="sc-leaf{R.choice([1, 2, 2, 3])}" cx="{f(x + dx * s)}" '
            f'cy="{f(y + dy * s)}" r="{f(r * s)}"/>'
        )
add("".join(trees))
grass = []
for _ in range(220):
    x = R.uniform(-20, W + 20)
    y = R.uniform(GROUND + 40, H)
    if abs(x - (door - 60 - (y - GROUND) * 0.6)) < 70:
        continue
    h = R.uniform(5, 12)
    grass.append(f"M{f(x)} {f(y)}l{f(R.uniform(-2, 2))} {f(-h)}")
add(f'<path class="sc-grass" fill="none" stroke-linecap="round" d="{"".join(grass)}"/>')
add(f'<rect class="sc-grainlayer" width="{W}" height="{H}" filter="url(#sc-grain)"/>')

SVG = (
    f'<svg class="sc-art" viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMax slice" '
    'focusable="false" '
    'aria-hidden="true" '
    f'xmlns="http://www.w3.org/2000/svg">' + "".join(out) + "</svg>"
)

if __name__ == "__main__":
    print(len(SVG))
