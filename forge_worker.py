import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from shortGPT.api_server import VIDEO_DIR, get_render_job, start_render_job

app = FastAPI(title="Shorts Forge Render Worker")


@app.get("/")
async def health():
    return {"status": "ok", "service": "shorts-forge-render-worker"}


@app.post("/api/render-jobs")
async def create_render_job(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.")
    job_id = start_render_job(
        str(payload.get("topic") or ""),
        str(payload.get("language") or "English"),
        str(payload.get("token") or ""),
        str(payload.get("storyboard") or ""),
    )
    return JSONResponse({"jobId": job_id, "status": "queued"})


@app.get("/api/render-jobs/{job_id}")
async def render_job_status(job_id: str):
    job = get_render_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Render job not found or expired.")
    result = {key: value for key, value in job.items() if key != "createdAt"}
    return JSONResponse(result)


@app.get("/gradio_api/file={file_path:path}")
@app.head("/gradio_api/file={file_path:path}")
async def download_rendered_video(file_path: str, request: Request):
    # Preserve the existing download URL shape while restricting reads to rendered videos.
    candidate = Path("/" + file_path.lstrip("/")).resolve()
    video_root = VIDEO_DIR.resolve()
    if candidate == video_root or video_root not in candidate.parents:
        raise HTTPException(status_code=404, detail="Video file not found.")
    if not candidate.is_file() or candidate.suffix.lower() != ".mp4":
        raise HTTPException(status_code=404, detail="Video file not found.")
    download = request.query_params.get("download") == "1"
    return FileResponse(
        str(candidate),
        media_type="video/mp4",
        filename=candidate.name,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"},
    )


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "31415")),
        log_level="info",
    )
