#!/usr/bin/env python3
"""Generate the FieldKit E2E test rig workflow image for the 2026-06-25 LinkedIn update."""

from PIL import Image, ImageDraw, ImageFont

# ── Canvas ──────────────────────────────────────────────────────────────────
W, H = 2547, 1422
img = Image.new("RGB", (W, H))
draw = ImageDraw.Draw(img)

# ── Palette (matches workflow-2026-05-31.jpg) ────────────────────────────────
BG        = (10,  14,  20)
CYAN      = (0,  215, 240)
BLUE      = (60, 130, 245)
GREEN     = (0,  220, 115)
YELLOW    = (255, 210,  0)
PURPLE    = (168,  85, 248)
WHITE     = (255, 255, 255)
LIGHT     = (200, 210, 220)
GRAY      = (110, 125, 145)
DARK_BOX  = (16,  26,  40)
RIG_FILL  = (0,   30,  48)

# ── Background ──────────────────────────────────────────────────────────────
draw.rectangle([0, 0, W, H], fill=BG)

# Subtle horizontal gradient bands (cosmetic)
for y in range(H):
    alpha = int(8 * (1 - y / H))
    draw.line([(0, y), (W, y)], fill=(alpha, alpha + 4, alpha + 10))

# ── Fonts (HelveticaNeue.ttc, matched to existing image) ────────────────────
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"
REG, BOLD, LIGHT_IDX, ULTRA, MED = 0, 1, 7, 5, 10

def f(size, variant=REG):
    return ImageFont.truetype(FONT, size, index=variant)

fTitle   = f(90, BOLD)
fSub     = f(40, LIGHT_IDX)
fCmd     = f(36, MED)
fNum     = f(64, BOLD)
fStage   = f(42, BOLD)
fActor   = f(32, MED)
fDetail  = f(28, REG)
fStatus  = f(30, MED)
fFooter  = f(28, LIGHT_IDX)

# ── Layout constants ─────────────────────────────────────────────────────────
MARGIN   = 110
BOX_W    = 390
BOX_H    = 430
N        = 5
GAP      = (W - 2 * MARGIN - N * BOX_W) // (N - 1)   # ~94px

STAGE_TOP = 500
STAGE_BOT = STAGE_TOP + BOX_H   # 930

# X positions (left edge of each box)
def bx(i):
    return MARGIN + i * (BOX_W + GAP)

def cx(i):
    return bx(i) + BOX_W // 2

# ── Stage data ───────────────────────────────────────────────────────────────
stages = [
    {
        "title":  "Generate\nClock Frames",
        "actor":  "FFmpeg · local",
        "detail": "MM/DD/YYYY HH:MM:SS\nadvancing per frame",
        "time":   "2s",
        "color":  CYAN,
    },
    {
        "title":  "Upload to\nGoogle Drive",
        "actor":  "Drive API",
        "detail": "9 JPEG frames\nproject folder structure",
        "time":   "14s",
        "color":  BLUE,
    },
    {
        "title":  "Generate Video\n+ Send Approval",
        "actor":  "process_photos.py · cron",
        "detail": "FFmpeg slideshow\nTelegram message sent",
        "time":   "42s",
        "color":  GREEN,
    },
    {
        "title":  "Admin\nApproves",
        "actor":  "Telegram · human gate",
        "detail": "one tap\nonly manual step",
        "time":   "56s",
        "color":  YELLOW,
    },
    {
        "title":  "Video\nGoes Live",
        "actor":  "Facebook Graph API",
        "detail": "upload_facebook.py · cron\npost confirmed",
        "time":   "55s",
        "color":  PURPLE,
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
#  HEADER
# ═══════════════════════════════════════════════════════════════════════════════

draw.text((W // 2, 88), "FieldKit — End-to-End Test Rig",
          font=fTitle, fill=WHITE, anchor="mm")

draw.text((W // 2, 160), "One command. Five stages. Full pipeline verified.",
          font=fSub, fill=GRAY, anchor="mm")

# Command pill
cmd_text = "$ python3 run_e2e_test.py --duration 30"
tw = draw.textlength(cmd_text, font=fCmd)
px, py = 30, 10
pill = [(W // 2 - tw // 2 - px, 198),
        (W // 2 + tw // 2 + px, 198 + 44 + py * 2)]
draw.rounded_rectangle(pill, radius=10, fill=(0, 28, 44), outline=CYAN, width=2)
draw.text((W // 2, 198 + 22 + py), cmd_text, font=fCmd, fill=CYAN, anchor="mm")


# ═══════════════════════════════════════════════════════════════════════════════
#  ORCHESTRATOR BAR
# ═══════════════════════════════════════════════════════════════════════════════

RIG_TOP = 355
RIG_BOT = RIG_TOP + 84

draw.rounded_rectangle(
    [MARGIN, RIG_TOP, W - MARGIN, RIG_BOT],
    radius=14, fill=RIG_FILL, outline=CYAN, width=2
)
draw.text(
    (W // 2, (RIG_TOP + RIG_BOT) // 2),
    "run_e2e_test.py   ·   orchestrator   ·   polls state.json & facebook_state.json   ·   reports pass / fail per stage",
    font=fDetail, fill=CYAN, anchor="mm"
)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONNECTOR LINES  (orchestrator → stage boxes)
# ═══════════════════════════════════════════════════════════════════════════════

for i in range(N):
    x = cx(i)
    draw.line([(x, RIG_BOT + 1), (x, STAGE_TOP - 1)], fill=(40, 65, 90), width=2)
    # Small arrow tip pointing down
    draw.polygon([(x - 8, STAGE_TOP - 14), (x + 8, STAGE_TOP - 14), (x, STAGE_TOP - 1)],
                 fill=(40, 65, 90))


# ═══════════════════════════════════════════════════════════════════════════════
#  STAGE → STAGE ARROWS
# ═══════════════════════════════════════════════════════════════════════════════

ARR_Y = STAGE_TOP + BOX_H // 2   # vertical midpoint of boxes

for i in range(N - 1):
    x1 = bx(i) + BOX_W + 6
    x2 = bx(i + 1) - 6
    mid_y = ARR_Y

    draw.line([(x1, mid_y), (x2 - 14, mid_y)], fill=LIGHT, width=3)
    # Arrowhead
    draw.polygon(
        [(x2 - 14, mid_y - 10), (x2, mid_y), (x2 - 14, mid_y + 10)],
        fill=LIGHT
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  STAGE BOXES
# ═══════════════════════════════════════════════════════════════════════════════

for i, stage in enumerate(stages):
    color = stage["color"]
    x = bx(i)
    box = [x, STAGE_TOP, x + BOX_W, STAGE_BOT]

    # Box background + border
    draw.rounded_rectangle(box, radius=22, fill=DARK_BOX, outline=color, width=3)

    # ── Number circle ──
    NUM_CY = STAGE_TOP + 60
    R = 38
    draw.ellipse([cx(i) - R, NUM_CY - R, cx(i) + R, NUM_CY + R], fill=color)
    draw.text((cx(i), NUM_CY), str(i + 1), font=fNum, fill=BG, anchor="mm")

    # ── Stage title (two lines) ──
    draw.multiline_text(
        (cx(i), STAGE_TOP + 148),
        stage["title"],
        font=fStage, fill=WHITE, anchor="mm", align="center", spacing=8
    )

    # ── Divider ──
    DIV_Y = STAGE_TOP + 228
    draw.line([(x + 24, DIV_Y), (x + BOX_W - 24, DIV_Y)], fill=(35, 55, 78), width=1)

    # ── Actor label ──
    draw.text((cx(i), STAGE_TOP + 265), stage["actor"],
              font=fActor, fill=color, anchor="mm")

    # ── Detail (two lines) ──
    draw.multiline_text(
        (cx(i), STAGE_TOP + 340),
        stage["detail"],
        font=fDetail, fill=GRAY, anchor="mm", align="center", spacing=6
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  TIMING ROW  (below stage boxes)
# ═══════════════════════════════════════════════════════════════════════════════

STATUS_Y = STAGE_BOT + 54

for i, stage in enumerate(stages):
    draw.text((cx(i), STATUS_Y), stage["time"],
              font=fStatus, fill=stage["color"], anchor="mm")


# ═══════════════════════════════════════════════════════════════════════════════
#  SUMMARY + FINDINGS
# ═══════════════════════════════════════════════════════════════════════════════

RULE_Y = STATUS_Y + 52
draw.line([(MARGIN, RULE_Y), (W - MARGIN, RULE_Y)], fill=(30, 45, 65), width=1)

SUM_Y = RULE_Y + 56
summary = "Total: 2m 49s   ·   363 unit tests   ·   zero false positives"
draw.text((W // 2, SUM_Y), summary, font=fActor, fill=LIGHT, anchor="mm")

# "What the rig found that unit tests missed" — three columns
FIND_TOP = SUM_Y + 72
fFindHead = f(28, BOLD)
fFindBody = f(26, REG)

findings = [
    ("Cron timing assumption",
     "Pipeline order shift silently\ndropped files mid-run"),
    ("State file path mismatch",
     "Cron user's working dir differed\nfrom local dev path"),
    ("File deleted too early",
     "_delete_local_file() moved from\ncheck_approval to upload_facebook"),
]

col_w = (W - 2 * MARGIN) // 3
for j, (head, body) in enumerate(findings):
    col_cx = MARGIN + col_w * j + col_w // 2

    # Dot
    draw.ellipse([col_cx - 5, FIND_TOP - 5, col_cx + 5, FIND_TOP + 5], fill=GRAY)
    draw.text((col_cx, FIND_TOP + 30), head, font=fFindHead, fill=LIGHT, anchor="mm")
    draw.multiline_text(
        (col_cx, FIND_TOP + 80), body,
        font=fFindBody, fill=GRAY, anchor="mm", align="center", spacing=5
    )
    # Vertical separator (between columns)
    if j < 2:
        sep_x = MARGIN + col_w * (j + 1)
        draw.line([(sep_x, FIND_TOP - 10), (sep_x, FIND_TOP + 120)],
                  fill=(30, 45, 65), width=1)

# Caption line
draw.text((W // 2, FIND_TOP + 148),
          "Caught by running the full chain — not by unit tests",
          font=fFindBody, fill=(80, 100, 120), anchor="mm")


# ═══════════════════════════════════════════════════════════════════════════════
#  FOOTER
# ═══════════════════════════════════════════════════════════════════════════════

FOOT_Y = H - 56
footer = "github.com/sandeepkesarkar/fieldkit   ·   #buildinpublic   ·   #specdriven"
draw.text((W // 2, FOOT_Y), footer, font=fFooter, fill=GRAY, anchor="mm")


# ═══════════════════════════════════════════════════════════════════════════════
#  SAVE
# ═══════════════════════════════════════════════════════════════════════════════

out = "/Users/sandeep_a_k/src/fieldkit/updates/workflow-2026-06-25.png"
img.save(out, dpi=(180, 180))
print(f"Saved: {out}")
