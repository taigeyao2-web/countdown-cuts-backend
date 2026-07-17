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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

import video_pipeline as vp

app = FastAPI(title="Countdown Cuts API")

# Allow the frontend (served from anywhere, e.g. a static host) to call this API.
# Tighten allow_origins to your real frontend domain before going to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_CLIPS = 15
MAX_FILE_SIZE_MB = 200


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/process")
async def process_video(
    title: str = Form(...),
    clips_json: str = Form(...),
    files: list[UploadFile] = File(...),
):
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

    if len(clip_meta) > MAX_CLIPS:
        raise HTTPException(400, f"Too many clips (max {MAX_CLIPS})")

    for item in clip_meta:
        if "rank" not in item or "caption" not in item:
            raise HTTPException(400, "Each clip entry needs 'rank' and 'caption'")

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
        vp.build_ranking_video(clips, title, output_path, tmp_dir)

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
