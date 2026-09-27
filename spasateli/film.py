"""Test pipeline for the "Fortuna" casino scene via OpenRouter.

Steps:
    python film.py keyframes [--shots 3,7] [--variants 2]   # still frames -> keyframes/
    python film.py submit    [--shots 3,7] [--takes 5]      # image-to-video jobs
    python film.py poll                                      # wait, download -> clips/
    python film.py concat    --pick 1:a,2:c,...              # glue chosen takes -> scene.mp4
    python film.py cost                                      # spent so far

Every submitted job ID is written to jobs.json before anything else happens,
so a dropped connection never leads to paying for the same generation twice.
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

API = "https://openrouter.ai/api/v1"
ROOT = Path(__file__).parent
SHOTS_FILE = ROOT / "shots.json"
JOBS_FILE = ROOT / "jobs.json"
KEYFRAMES = ROOT / "keyframes"
CLIPS = ROOT / "clips"

IMAGE_MODEL = os.getenv("IMAGE_MODEL", "bytedance-seed/seedream-5-0-pro")
VIDEO_MODEL = os.getenv("VIDEO_MODEL", "bytedance/seedance-2.0-mini")
RESOLUTION = os.getenv("VIDEO_RESOLUTION", "480p")
ASPECT = "16:9"
# Public base URL where keyframes/ is hosted. Without it frames are sent as
# data URLs, which some video providers refuse.
KEYFRAME_BASE_URL = os.getenv("KEYFRAME_BASE_URL", "").rstrip("/")
POLL_INTERVAL = 20
TERMINAL = {"completed", "failed", "cancelled", "expired"}


def client() -> httpx.Client:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        sys.exit("Set OPENROUTER_API_KEY")
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(60, read=300),
    )


def load_shots(only: str | None) -> tuple[str, list[dict]]:
    data = json.loads(SHOTS_FILE.read_text())
    shots = data["shots"]
    if only:
        wanted = {int(x) for x in only.split(",")}
        shots = [s for s in shots if s["id"] in wanted]
    return data["style"], shots


def load_jobs() -> list[dict]:
    return json.loads(JOBS_FILE.read_text()) if JOBS_FILE.exists() else []


def save_jobs(jobs: list[dict]) -> None:
    tmp = JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(jobs, indent=2, ensure_ascii=False))
    tmp.replace(JOBS_FILE)


def keyframe_path(shot_id: int) -> Path:
    return KEYFRAMES / f"shot_{shot_id:02d}.png"


def data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def keyframe_url(shot_id: int) -> str:
    path = keyframe_path(shot_id)
    if KEYFRAME_BASE_URL:
        return f"{KEYFRAME_BASE_URL}/{path.name}"
    return data_url(path)


def cmd_keyframes(args) -> None:
    style, shots = load_shots(args.shots)
    KEYFRAMES.mkdir(exist_ok=True)
    refs = [
        {"type": "image_url", "image_url": {"url": data_url(Path(p))}}
        for p in (args.ref or [])
    ]
    total = 0.0
    with client() as c:
        for shot in shots:
            for v in range(args.variants):
                payload = {
                    "model": IMAGE_MODEL,
                    "prompt": f"{shot['image']}. {style}",
                    "aspect_ratio": ASPECT,
                    "resolution": "1K",
                }
                if refs:
                    payload["prompt"] = (
                        "Keep the characters' faces, hair and costumes exactly as in the reference image. "
                        + payload["prompt"]
                    )
                    payload["input_references"] = refs
                r = c.post("/images", json=payload)
                if r.status_code != 200:
                    print(f"shot {shot['id']}: HTTP {r.status_code} {r.text[:300]}")
                    break
                body = r.json()
                total += (body.get("usage") or {}).get("cost") or 0
                img = base64.b64decode(body["data"][0]["b64_json"])
                out = KEYFRAMES / f"shot_{shot['id']:02d}_{args.tag}{v + 1}.png"
                out.write_bytes(img)
                print(f"shot {shot['id']}: {out.name}")
    print(f"images cost: ${total:.3f}")
    print(f"Pick the best variant of each shot and copy it to shot_NN.png in {KEYFRAMES}/")


def cmd_submit(args) -> None:
    style, shots = load_shots(args.shots)
    missing = [s["id"] for s in shots if not keyframe_path(s["id"]).exists()]
    if missing:
        sys.exit(f"No keyframes for shots {missing}: expected {KEYFRAMES}/shot_NN.png")
    if not KEYFRAME_BASE_URL:
        print("KEYFRAME_BASE_URL not set: sending keyframes as data URLs")

    jobs = load_jobs()
    with client() as c:
        for shot in shots:
            prompt = f"{shot['motion']}. {style}"
            if shot.get("audio"):
                prompt += f". Sound: {shot['audio']}"
            for _ in range(args.takes):
                r = c.post("/videos", json={
                    "model": VIDEO_MODEL,
                    "prompt": prompt,
                    "duration": shot["duration"],
                    "resolution": RESOLUTION,
                    "aspect_ratio": ASPECT,
                    "generate_audio": True,
                    "frame_images": [{
                        "type": "image_url",
                        "image_url": {"url": keyframe_url(shot["id"])},
                        "frame_type": "first_frame",
                    }],
                })
                if r.status_code not in (200, 202):
                    print(f"shot {shot['id']}: HTTP {r.status_code} {r.text[:300]}")
                    break
                job = r.json()
                jobs.append({
                    "shot": shot["id"],
                    "id": job["id"],
                    "status": job["status"],
                    "model": VIDEO_MODEL,
                    "submitted": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
                save_jobs(jobs)
                print(f"shot {shot['id']}: job {job['id']}")


def cmd_poll(args) -> None:
    CLIPS.mkdir(exist_ok=True)
    jobs = load_jobs()
    with client() as c:
        while True:
            pending = [j for j in jobs if j["status"] not in TERMINAL]
            for j in pending:
                r = c.get(f"/videos/{j['id']}")
                if r.status_code != 200:
                    print(f"{j['id']}: HTTP {r.status_code}")
                    continue
                body = r.json()
                j["status"] = body["status"]
                j["cost"] = (body.get("usage") or {}).get("cost")
                if body.get("error"):
                    j["error"] = body["error"]
                save_jobs(jobs)
                if j["status"] == "completed":
                    download(c, j, jobs)
                elif j["status"] in TERMINAL:
                    print(f"shot {j['shot']}: {j['id']} {j['status']} {j.get('error', '')}")
            # Retry downloads that failed earlier.
            for j in jobs:
                if j["status"] == "completed" and not j.get("file"):
                    download(c, j, jobs)
            if not any(j["status"] not in TERMINAL for j in jobs):
                break
            time.sleep(POLL_INTERVAL)
    report(jobs)


def download(c: httpx.Client, job: dict, jobs: list[dict]) -> None:
    take = sum(1 for j in jobs if j["shot"] == job["shot"] and j.get("file"))
    out = CLIPS / f"shot_{job['shot']:02d}_{chr(ord('a') + take)}.mp4"
    r = c.get(f"/videos/{job['id']}/content", params={"index": 0}, follow_redirects=True)
    if r.status_code != 200:
        print(f"{job['id']}: download HTTP {r.status_code}")
        return
    out.write_bytes(r.content)
    job["file"] = str(out.relative_to(ROOT))
    save_jobs(jobs)
    print(f"shot {job['shot']}: {out.name}")


def report(jobs: list[dict]) -> None:
    spent = sum(j.get("cost") or 0 for j in jobs)
    done = sum(1 for j in jobs if j["status"] == "completed")
    print(f"jobs: {len(jobs)}, completed: {done}, video cost: ${spent:.2f}")


def cmd_cost(args) -> None:
    report(load_jobs())


def cmd_concat(args) -> None:
    # --pick 1:a,2:c,... ; shots not listed use take "a"
    _, shots = load_shots(None)
    picks = dict(p.split(":") for p in args.pick.split(",")) if args.pick else {}
    files = []
    for s in shots:
        f = CLIPS / f"shot_{s['id']:02d}_{picks.get(str(s['id']), 'a')}.mp4"
        if not f.exists():
            sys.exit(f"missing {f}")
        files.append(f)
    listing = ROOT / "list.txt"
    listing.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    # Re-encode: takes from different jobs may differ in fps or audio params.
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(ROOT / "scene.mp4"),
    ], check=True)
    print(f"-> {ROOT / 'scene.mp4'}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keyframes")
    k.add_argument("--shots", help="comma-separated shot ids, default all")
    k.add_argument("--variants", type=int, default=2)
    k.add_argument("--ref", action="append", help="reference image for character consistency, repeatable")
    k.add_argument("--tag", default="v", help="variant file prefix, e.g. r -> shot_07_r1.png")
    k.set_defaults(func=cmd_keyframes)

    s = sub.add_parser("submit")
    s.add_argument("--shots", help="comma-separated shot ids, default all")
    s.add_argument("--takes", type=int, default=5)
    s.set_defaults(func=cmd_submit)

    sub.add_parser("poll").set_defaults(func=cmd_poll)
    sub.add_parser("cost").set_defaults(func=cmd_cost)

    c = sub.add_parser("concat")
    c.add_argument("--pick", help="shot:take pairs, e.g. 1:a,2:c")
    c.set_defaults(func=cmd_concat)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
