"""
main.py - FastAPI backend for Countdown Cuts.

Endpoints:
  POST /api/process
      multipart/form-data:
        - title: str
        - clips_json: str (JSON array, in PLAY ORDER worst->best):
              [{"rank": 5, "caption": "...", "trim_seconds": 5}, ...]
        - files: one file per clip, SAME ORDER as clips_json

      Returns: the finished .mp4 as a file download.

  GET /api/health
      Basic health check.

Run locally with:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import os
import json
import shutil
import tempfile
import uuid

import stripe
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

import video_pipeline as vp

app = FastAPI(title="Countdown Cuts API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------------
# Pricing tiers. "free" needs no payment. Paid tiers require a Stripe
# Checkout session id (created via a Stripe Payment Link) whose payment
# status we verify server-side before allowing the larger clip limit.
# ----------------------------------------------------------------------
TIERS = {
    "free": {"max_clips": 3},
    "full": {"max_clips": 10},
    "premium": {"max_clips": 15},
}

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")  # set this in Render's environment variables, never in code

# In-memory set of Stripe session IDs already used to generate a video.
# NOTE: this resets if the server restarts and won't work across multiple
# server instances - fine for a single free-tier Render instance, but if
# you scale to multiple instances you'd want a shared store (e.g. Redis)
# instead.
_used_sessions = set()

MAX_FILE_SIZE_MB = 200


@app.get("/api/health")
def health():
    return {"status": "ok"}


def verify_paid_session(tier: str, session_id: str | None):
    """Raise HTTPException if this tier requires payment and the session
    isn't a valid, unused, paid Stripe Checkout session."""
    if tier == "free":
        return

    if tier not in TIERS:
        raise HTTPException(400, f"Unknown tier '{tier}'")

    if not session_id:
        raise HTTPException(402, f"The '{tier}' tier requires a completed payment (missing session_id)")

    if session_id in _used_sessions:
        raise HTTPException(402, "This payment has already been used for a previous video")

    if not stripe.api_key:
        raise HTTPException(500, "Server isn't configured with a Stripe secret key")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        raise HTTPException(402, f"Couldn't verify payment session: {e}")

    if session.payment_status != "paid":
        raise HTTPException(402, "Payment for this session was not completed")

    _used_sessions.add(session_id)


@app.post("/api/process")
async def process_video(
    title: str = Form(...),
    clips_json: str = Form(...),
    tier: str = Form("free"),
    session_id: str | None = Form(None),
    font: str = Form("liberation"),
    title_accent_color: str = Form("#29ABE2"),
    title_secondary_color: str = Form("#FFC107"),
    list_color_mode: str = Form("auto"),
    list_custom_color: str = Form("white"),
    border_enabled: bool = Form(False),
    border_width: int = Form(6),
    border_color: str = Form("black"),
    special_requests: str = Form(""),
    files: list[UploadFile] = File(...),
):
    if tier not in TIERS:
        raise HTTPException(400, f"Unknown tier '{tier}'")

    verify_paid_session(tier, session_id)

    try:
        clip_meta = json.loads(clips_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "clips_json is not valid JSON")

    if not isinstance(clip_meta, list) or not clip_meta:
        raise HTTPException(400, "clips_json must be a non-empty JSON array")

    if len(clip_meta) != len(files):
        raise HTTPException(
            400,
            f"Mismatch: {len(clip_meta)} clip entries but {len(files)} files uploaded",
        )

    max_clips = TIERS[tier]["max_clips"]
    if len(clip_meta) > max_clips:
        raise HTTPException(
            400,
            f"The '{tier}' tier allows up to {max_clips} clips (you sent {len(clip_meta)}). Upgrade for more.",
        )

    for item in clip_meta:
        if "rank" not in item or "caption" not in item:
            raise HTTPException(400, "Each clip entry needs 'rank' and 'caption'")

    if font not in vp.FONT_CHOICES:
        raise HTTPException(400, f"Unknown font '{font}'. Choices: {list(vp.FONT_CHOICES)}")

    style = {
        "font": font,
        "title_accent_color": title_accent_color,
        "title_secondary_color": title_secondary_color,
        "list_color_mode": list_color_mode,
        "list_custom_color": list_custom_color,
        "border_enabled": border_enabled,
        "border_width": border_width,
        "border_color": border_color,
    }

    # special_requests isn't used by the automated pipeline (there's no human
    # to read it) - it's accepted so the frontend can collect it, e.g. for
    # your own records or to extend later (custom fonts, manual touch-ups, etc).
    # Logged here so you can see it in Render's logs if you want to review requests.
    if special_requests.strip():
        print(f"[special_requests] tier={tier} title={title!r}: {special_requests!r}")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(tempfile.gettempdir(), "countdown_cuts", job_id)
    uploads_dir = os.path.join(job_dir, "uploads")
    tmp_dir = os.path.join(job_dir, "tmp")
    os.makedirs(uploads_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)

    try:
        clips = []
        for idx, (meta, upload) in enumerate(zip(clip_meta, files)):
            dest_path = os.path.join(uploads_dir, f"clip_{idx:03d}_{upload.filename}")

            size = 0
            with open(dest_path, "wb") as out_f:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_SIZE_MB * 1024 * 1024:
                        raise HTTPException(
                            400, f"{upload.filename} exceeds {MAX_FILE_SIZE_MB}MB limit"
                        )
                    out_f.write(chunk)

            clips.append(
                {
                    "path": dest_path,
                    "rank": int(meta["rank"]),
                    "caption": str(meta["caption"]),
                    "trim_seconds": meta.get("trim_seconds"),
                }
            )

        # Play order = order the clips were submitted in (worst -> best), per rank field
        clips.sort(key=lambda c: -c["rank"])  # highest rank number (worst) plays first

        output_path = os.path.join(job_dir, "final_ranked_video.mp4")
        vp.build_ranking_video(clips, title, output_path, tmp_dir, style=style)

        if not os.path.isfile(output_path):
            raise HTTPException(500, "Processing finished but no output file was produced")

        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename="final_ranked_video.mp4",
        )

    except vp.PipelineError as e:
        raise HTTPException(500, f"Video processing failed: {e}")
    finally:
        # Clean up uploads/intermediate files; keep final output only long enough to send it.
        # (FileResponse streams from disk, so in a production app you'd clean this up
        # in a background task after the response is sent instead of immediately here.)
        pass
