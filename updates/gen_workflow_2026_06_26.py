#!/usr/bin/env python3
"""
Generate combined workflow image for the 2026-06-26 LinkedIn update.
Top half: core FieldKit pipeline (user story).
Bottom half: e2e test rig (Feature 004) that verified it.
"""

from PIL import Image, ImageDraw, ImageFont
import math

# ── Canvas ────────────────────────────────────────────────────────────────────
W, H = 2547, 2220
img = Image.new("RGB", (W, H))
draw = ImageDraw.Draw(img)

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = (10,  14,  20)
CYAN     = (0,  215, 240)
BLUE     = (60, 130, 245)
GREEN    = (0,  220, 115)
YELLOW   = (255, 210,  0)
PURPLE   = (168,  85, 248)
WHITE    = (255, 255, 255)
LIGHT    = (200, 210, 220)
GRAY     = (110, 125, 145)
DARK_BOX = (16,  26,  40)
RIG_FILL = (0,   28,  46)

# ── Background ────────────────────────────────────────────────────────────────
draw.rectangle([0, 0, W, H], fill=BG)
for y in range(H):
    alpha = int(6 * (1 - y / H))
    draw.line([(0, y), (W, y)], fill=(alpha, alpha + 4, alpha + 10))

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT = "/System/Library/Fonts/HelveticaNeue.ttc"
REG, BOLD, LIGHT_IDX, MED = 0, 1, 7, 10

def f(size, variant=REG):
    return ImageFont.truetype(FONT, size, index=variant)

fTitle    = f(86, BOLD)
fSub      = f(38, LIGHT_IDX)
fSec      = f(46, BOLD)
fSecSub   = f(32, LIGHT_IDX)
fColHead  = f(32, BOLD)
fNode     = f(36, BOLD)
fNodeSub  = f(26, REG)
fArrow    = f(24, LIGHT_IDX)
fCmd      = f(34, MED)
fStage    = f(38, BOLD)
fActor    = f(30, MED)
fDetail   = f(26, REG)
fNum      = f(58, BOLD)
fStatus   = f(28, MED)
fFindHead = f(27, BOLD)
fFindBody = f(25, REG)
fFooter   = f(26, LIGHT_IDX)

# ── Shared helpers ────────────────────────────────────────────────────────────
def node(cx, cy, title, subtitle, color, w=400, h=108):
    x0, y0 = cx - w // 2, cy - h // 2
    x1, y1 = cx + w // 2, cy + h // 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=15, fill=DARK_BOX, outline=color, width=2)
    draw.text((cx, cy - 15), title,    font=fNode,    fill=color, anchor="mm")
    draw.text((cx, cy + 21), subtitle, font=fNodeSub, fill=GRAY,  anchor="mm")
    return x0, y0, x1, y1

def arw(x1, y1, x2, y2, label="", color=LIGHT):
    draw.line([(x1, y1), (x2, y2)], fill=color, width=2)
    dx, dy = x2 - x1, y2 - y1
    L = math.hypot(dx, dy)
    if L < 1:
        return
    ux, uy = dx / L, dy / L
    px, py = -uy, ux
    S = 13
    tip = (int(x2), int(y2))
    a   = (int(x2 - ux*S + px*S*0.5), int(y2 - uy*S + py*S*0.5))
    b   = (int(x2 - ux*S - px*S*0.5), int(y2 - uy*S - py*S*0.5))
    draw.polygon([tip, a, b], fill=color)
    if label:
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        if abs(dx) >= abs(dy):
            draw.text((mx, my - 18), label, font=fArrow, fill=GRAY, anchor="mm")
        else:
            draw.text((mx + 10, my), label, font=fArrow, fill=GRAY, anchor="lm")


# ══════════════════════════════════════════════════════════════════════════════
#  HEADER
# ══════════════════════════════════════════════════════════════════════════════

draw.text((W // 2, 82), "FieldKit — How It Works",
          font=fTitle, fill=WHITE, anchor="mm")
draw.text((W // 2, 154),
          "Photos  →  AI-generated video  →  Human approval  →  Facebook post",
          font=fSub, fill=GRAY, anchor="mm")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — CORE PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

MARGIN   = 60
COL_TOP  = 218
COL_BOT  = 990

LC_W, MC_W, RC_W = 500, 1080, 500
GAP_COL = (W - 2 * MARGIN - LC_W - MC_W - RC_W) // 2

LC_X = MARGIN
MC_X = LC_X + LC_W + GAP_COL
RC_X = MC_X + MC_W + GAP_COL

LC_CX = LC_X + LC_W // 2
MC_CX = MC_X + MC_W // 2
RC_CX = RC_X + RC_W // 2

# Column borders
draw.rounded_rectangle([LC_X, COL_TOP, LC_X+LC_W, COL_BOT], radius=18, outline=CYAN,   width=2)
draw.rounded_rectangle([MC_X, COL_TOP, MC_X+MC_W, COL_BOT], radius=18, outline=BLUE,   width=2)
draw.rounded_rectangle([RC_X, COL_TOP, RC_X+RC_W, COL_BOT], radius=18, outline=PURPLE, width=2)

draw.text((LC_CX, COL_TOP + 34), "Field / Client",          font=fColHead, fill=CYAN,   anchor="mm")
draw.text((MC_CX, COL_TOP + 34), "Mac Mini  (self-hosted)", font=fColHead, fill=BLUE,   anchor="mm")
draw.text((RC_CX, COL_TOP + 34), "Social Media",            font=fColHead, fill=PURPLE, anchor="mm")

# OpenClaw orchestrator bar
OC_CY = 330
OC_T, OC_B = OC_CY - 42, OC_CY + 42
draw.rounded_rectangle(
    [MC_X + 26, OC_T, MC_X + MC_W - 26, OC_B],
    radius=12, fill=(0, 20, 38), outline=BLUE, width=2
)
draw.text((MC_CX, OC_CY - 11), "OpenClaw",
          font=f(36, BOLD), fill=CYAN, anchor="mm")
draw.text((MC_CX, OC_CY + 23),
          "orchestrator   ·   watches Google Drive   ·   runs pipeline scripts",
          font=fArrow, fill=BLUE, anchor="mm")

# Node Y positions
BIZ_Y = 360
GDR_Y = 548
ADM_Y = 808

PRO_Y = 515
VID_Y = 696
UPL_Y = 884

TEL_Y = 658
FB_Y  = 884

NW = 750   # middle column node width

# Left column nodes
node(LC_CX, BIZ_Y, "Business Owner", "photos the job site",   CYAN)
node(LC_CX, GDR_Y, "Google Drive",   "shared project folder", CYAN)
node(LC_CX, ADM_Y, "Admin",          "Telegram · HITL gate",  YELLOW)

# Middle column nodes
node(MC_CX, PRO_Y, "process_photos.py", "AI privacy scrub",  BLUE,   w=NW)
node(MC_CX, VID_Y, "generate_video",    "FFmpeg · 9:16 MP4", GREEN,  w=NW)
node(MC_CX, UPL_Y, "upload_to_facebook","Meta Graph API",    PURPLE, w=NW)

# Right column nodes
node(RC_CX, TEL_Y, "Telegram",     "notifies Admin",  YELLOW)
node(RC_CX, FB_Y,  "Facebook Page","video published",  PURPLE)

# Arrows — section 1
NH = 54   # node half-height (108/2)
arw(LC_CX, BIZ_Y + NH, LC_CX, GDR_Y - NH, "uploads photos")
arw(LC_X + LC_W + 2, GDR_Y, MC_X - 2, PRO_Y, "media")
arw(MC_CX, OC_B + 2, MC_CX, PRO_Y - NH, "triggers")
arw(MC_CX, PRO_Y + NH, MC_CX, VID_Y - NH, "scrubbed frames")
arw(MC_X - 2, VID_Y, LC_X + LC_W + 2, ADM_Y, "approval request")
arw(LC_X + LC_W + 2, ADM_Y, MC_X - 2, UPL_Y, "approved")
arw(MC_X + MC_W + 2, UPL_Y, RC_X - 2, FB_Y, "Graph API")
arw(RC_CX, FB_Y - NH, RC_CX, TEL_Y + NH, "post live")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION DIVIDER
# ══════════════════════════════════════════════════════════════════════════════

DIV_Y = 1032
draw.line([(MARGIN, DIV_Y), (W - MARGIN, DIV_Y)], fill=(28, 46, 68), width=1)

draw.text((W // 2, DIV_Y + 46), "End-to-End Test Rig — verifying the full chain",
          font=fSec, fill=LIGHT, anchor="mm")
draw.text((W // 2, DIV_Y + 92),
          "One command. Five stages. No manual setup.",
          font=fSecSub, fill=GRAY, anchor="mm")

# Command pill
cmd_text = "$ python3 run_e2e_test.py --duration 30"
tw = draw.textlength(cmd_text, font=fCmd)
px, py = 28, 8
pill = [(W//2 - tw//2 - px, DIV_Y + 116),
        (W//2 + tw//2 + px, DIV_Y + 116 + 42 + py * 2)]
draw.rounded_rectangle(pill, radius=10, fill=(0, 26, 42), outline=CYAN, width=2)
draw.text((W // 2, DIV_Y + 137 + py), cmd_text, font=fCmd, fill=CYAN, anchor="mm")


# ══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — TEST RIG STAGES
# ══════════════════════════════════════════════════════════════════════════════

# Orchestrator bar
RIG_TOP = DIV_Y + 206
RIG_BOT = RIG_TOP + 72
draw.rounded_rectangle(
    [MARGIN, RIG_TOP, W - MARGIN, RIG_BOT],
    radius=12, fill=RIG_FILL, outline=CYAN, width=2
)
draw.text(
    (W // 2, (RIG_TOP + RIG_BOT) // 2),
    "run_e2e_test.py   ·   orchestrator   ·   polls state.json & facebook_state.json   ·   reports pass / fail per stage",
    font=fDetail, fill=CYAN, anchor="mm"
)

# Stage boxes
STAGE_TOP = RIG_BOT + 60
BOX_W     = 390
BOX_H     = 400
N         = 5
GAP_STAGE = (W - 2 * MARGIN - N * BOX_W) // (N - 1)

def bx(i): return MARGIN + i * (BOX_W + GAP_STAGE)
def cx_s(i): return bx(i) + BOX_W // 2

STAGE_BOT = STAGE_TOP + BOX_H

stages = [
    {"title": "Generate\nClock Frames", "actor": "FFmpeg · local",
     "detail": "MM/DD/YYYY HH:MM:SS\nadvancing per frame", "time": "2s",  "color": CYAN},
    {"title": "Upload to\nGoogle Drive",  "actor": "Drive API",
     "detail": "9 JPEG frames\nproject folder structure",  "time": "14s", "color": BLUE},
    {"title": "Generate Video\n+ Send Approval", "actor": "process_photos.py · cron",
     "detail": "FFmpeg slideshow\nTelegram message sent",  "time": "42s", "color": GREEN},
    {"title": "Admin\nApproves",           "actor": "Telegram · human gate",
     "detail": "one tap\nonly manual step",                "time": "56s", "color": YELLOW},
    {"title": "Video\nGoes Live",          "actor": "Facebook Graph API",
     "detail": "upload_facebook.py · cron\npost confirmed","time": "55s", "color": PURPLE},
]

# Connector lines (orchestrator → stage boxes)
for i in range(N):
    x = cx_s(i)
    draw.line([(x, RIG_BOT + 1), (x, STAGE_TOP - 1)], fill=(38, 62, 88), width=2)
    draw.polygon([(x-8, STAGE_TOP-13), (x+8, STAGE_TOP-13), (x, STAGE_TOP-1)], fill=(38, 62, 88))

# Stage → stage arrows
ARR_Y = STAGE_TOP + BOX_H // 2
for i in range(N - 1):
    x1 = bx(i) + BOX_W + 6
    x2 = bx(i + 1) - 6
    draw.line([(x1, ARR_Y), (x2 - 13, ARR_Y)], fill=LIGHT, width=3)
    draw.polygon([(x2-13, ARR_Y-9), (x2, ARR_Y), (x2-13, ARR_Y+9)], fill=LIGHT)

# Stage boxes
for i, s in enumerate(stages):
    color = s["color"]
    x = bx(i)
    box = [x, STAGE_TOP, x + BOX_W, STAGE_BOT]
    draw.rounded_rectangle(box, radius=20, fill=DARK_BOX, outline=color, width=3)

    # Number circle
    NUM_CY = STAGE_TOP + 56
    R = 34
    draw.ellipse([cx_s(i)-R, NUM_CY-R, cx_s(i)+R, NUM_CY+R], fill=color)
    draw.text((cx_s(i), NUM_CY), str(i+1), font=fNum, fill=BG, anchor="mm")

    # Title
    draw.multiline_text((cx_s(i), STAGE_TOP + 138), s["title"],
                        font=fStage, fill=WHITE, anchor="mm", align="center", spacing=6)

    # Divider
    DIV2_Y = STAGE_TOP + 210
    draw.line([(x+22, DIV2_Y), (x+BOX_W-22, DIV2_Y)], fill=(34, 52, 74), width=1)

    # Actor
    draw.text((cx_s(i), STAGE_TOP + 248), s["actor"], font=fActor, fill=color, anchor="mm")

    # Detail
    draw.multiline_text((cx_s(i), STAGE_TOP + 318), s["detail"],
                        font=fDetail, fill=GRAY, anchor="mm", align="center", spacing=5)

# Timing row
TIM_Y = STAGE_BOT + 50
for i, s in enumerate(stages):
    draw.text((cx_s(i), TIM_Y), s["time"], font=fStatus, fill=s["color"], anchor="mm")

# Rule + summary
RULE2_Y = TIM_Y + 48
draw.line([(MARGIN, RULE2_Y), (W - MARGIN, RULE2_Y)], fill=(28, 46, 68), width=1)

SUM_Y = RULE2_Y + 52
draw.text((W // 2, SUM_Y),
          "Total: 2m 49s   ·   363 unit tests   ·   zero false positives",
          font=fActor, fill=LIGHT, anchor="mm")

# Findings (3 columns)
FIND_TOP = SUM_Y + 68
col_w = (W - 2 * MARGIN) // 3
findings = [
    ("Cron timing assumption",   "Pipeline order shift silently\ndropped files mid-run"),
    ("State file path mismatch", "Cron user's working dir differed\nfrom local dev path"),
    ("File deleted too early",   "_delete_local_file() fired before\nupload_facebook had finished"),
]
for j, (head, body) in enumerate(findings):
    ccx = MARGIN + col_w * j + col_w // 2
    draw.ellipse([ccx-5, FIND_TOP-5, ccx+5, FIND_TOP+5], fill=GRAY)
    draw.text((ccx, FIND_TOP + 28), head, font=fFindHead, fill=LIGHT, anchor="mm")
    draw.multiline_text((ccx, FIND_TOP + 74), body,
                        font=fFindBody, fill=GRAY, anchor="mm", align="center", spacing=5)
    if j < 2:
        sx = MARGIN + col_w * (j + 1)
        draw.line([(sx, FIND_TOP - 10), (sx, FIND_TOP + 108)], fill=(28, 46, 68), width=1)

draw.text((W // 2, FIND_TOP + 138),
          "Caught by running the full chain — not by unit tests",
          font=fFindBody, fill=(78, 98, 118), anchor="mm")


# ══════════════════════════════════════════════════════════════════════════════
#  FOOTER
# ══════════════════════════════════════════════════════════════════════════════

draw.text((W // 2, H - 52),
          "github.com/sandeepkesarkar/fieldkit   ·   #buildinpublic   ·   #specdriven",
          font=fFooter, fill=GRAY, anchor="mm")


# ── Save ──────────────────────────────────────────────────────────────────────
out = "/Users/sandeep_a_k/src/fieldkit/updates/workflow-2026-06-26.png"
img.save(out, dpi=(180, 180))
print(f"Saved: {out}")
