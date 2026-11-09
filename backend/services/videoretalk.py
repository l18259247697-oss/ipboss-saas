"""Beijing VideoRetalk adapter; one submission, persisted task, explicit resume."""
import hashlib
import json
import time
import uuid
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from services import media

BASE = "https://dashscope.aliyuncs.com/api/v1"
MODEL = "videoretalk"


class PendingTask(RuntimeError):
    pass


def _write_state(path, state):
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    pending.replace(path)


def validate_face(face_path):
    if Path(face_path).suffix.lower() not in {".mp4", ".mov", ".avi"}:
        raise RuntimeError("VideoRetalk 需要人物视频，不能使用照片代替")
    info = media.probe(face_path, "video")
    stream = info["stream"]
    fps = float(Fraction(stream.get("avg_frame_rate") or stream.get("r_frame_rate", "0/1")))
    if not 2 < info["duration"] < 120 or Path(face_path).stat().st_size > 300 * 1024**2:
        raise RuntimeError("视频须大于 2 秒、小于 120 秒，且不超过 300 MB")
    if not 15 <= fps <= 60 or not all(640 <= stream.get(k, 0) <= 2048 for k in ("width", "height")):
        raise RuntimeError("视频须为 15–60 帧，宽高均为 640–2048 像素；请使用原始清晰底版")
    if stream.get("codec_name") not in {"h264", "hevc"}:
        raise RuntimeError("视频编码须为 H.264 或 H.265")
    return info


def _upload(client, path, headers, model=MODEL):
    response = client.get(BASE + "/uploads", headers=headers,
                          params={"action": "getPolicy", "model": model})
    response.raise_for_status()
    policy = response.json()["data"]
    host = urlsplit(policy["upload_host"])
    if host.scheme != "https" or not (host.hostname or "").endswith(".aliyuncs.com"):
        raise RuntimeError("临时上传地址不属于阿里云，已停止上传")
    path = Path(path)
    key = policy["upload_dir"].rstrip("/") + "/" + uuid.uuid4().hex + path.suffix
    form = {"OSSAccessKeyId": policy["oss_access_key_id"], "policy": policy["policy"],
            "Signature": policy["signature"], "key": key,
            "x-oss-object-acl": policy["x_oss_object_acl"],
            "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
            "success_action_status": "200"}
    with path.open("rb") as source:
        uploaded = client.post(policy["upload_host"], data=form,
                               files={"file": (path.name, source, "application/octet-stream")})
    uploaded.raise_for_status()
    return "oss://" + key


def _input_hash(*paths):
    digest = hashlib.sha256()
    for path in paths:
        file_digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                file_digest.update(block)
        digest.update(file_digest.digest())
    return digest.hexdigest()


def generate(face_path, audio_path, out_path, cfg, *, video_extension=True):
    if not cfg.get("api_key"):
        raise RuntimeError("请填写阿里云百炼北京地域 DASHSCOPE_API_KEY")
    face_info = validate_face(face_path)
    duration = media.probe(audio_path, "audio")["duration"]
    if not 2 < duration < 120 or Path(audio_path).stat().st_size > 30 * 1024**2:
        raise RuntimeError("配音须大于 2 秒、小于 120 秒，且不超过 30 MB")
    if not video_extension and face_info["duration"] + 1e-6 < duration:
        raise RuntimeError("连续人物分段短于配音，未提交会截断口播的任务")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    state_file = out_path.parent / "videoretalk-task.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    fingerprint = hashlib.sha256((_input_hash(face_path, audio_path) + MODEL + cfg["api_key"]
                                  + str(video_extension)).encode()).hexdigest()
    if state and ((state.get("fingerprint") and state["fingerprint"] != fingerprint)
                  or (not state.get("fingerprint") and not video_extension)):
        raise PendingTask("口型输入或分段参数已改变，未复用或重新提交旧任务")
    if state.get("status") == "rejected":
        raise RuntimeError("原口型提交已被拒绝，未自动重复提交")
    headers = {"Authorization": "Bearer " + cfg["api_key"]}
    with httpx.Client(timeout=120) as client:
        if not state.get("task_id"):
            if state.get("status") == "submitting":
                raise PendingTask("口型提交结果未知，已停止自动重试；需在供应商控制台核对，避免重复生成")
            video_url = _upload(client, face_path, headers)
            audio_url = _upload(client, audio_path, headers)
            state = {"status": "submitting", "model": MODEL, "fingerprint": fingerprint,
                     "video_extension": video_extension}
            _write_state(state_file, state)
            try:
                response = client.post(BASE + "/services/aigc/image2video/video-synthesis",
                    headers={**headers, "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
                    json={"model": MODEL, "input": {"video_url": video_url, "audio_url": audio_url},
                          "parameters": {"video_extension": video_extension}})
            except httpx.RequestError as exc:
                raise PendingTask("口型提交连接中断，结果未知，未自动重复提交") from exc
            if response.status_code >= 500:
                raise PendingTask("口型服务提交结果未知，未自动重复提交")
            if response.is_error:
                state.update(status="rejected", http_status=response.status_code)
                _write_state(state_file, state)
                raise RuntimeError(f"口型服务拒绝任务（HTTP {response.status_code}），请核对账号权限与余额")
            task_id = response.json().get("output", {}).get("task_id")
            if not task_id:
                raise PendingTask("口型接口未返回任务编号，需核对供应商记录，未自动重试")
            state.update(task_id=task_id, status="PENDING")
            _write_state(state_file, state)
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            try:
                response = client.get(BASE + "/tasks/" + state["task_id"], headers=headers)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise PendingTask("口型任务已提交，查询暂时失败；可继续查询原任务，不会重新生成") from exc
            output = result.get("output", {})
            state.update(status=output.get("task_status"), usage=result.get("usage", {}))
            _write_state(state_file, state)
            if state["status"] == "SUCCEEDED":
                url = output.get("video_url")
                if not url:
                    raise PendingTask("口型任务成功但暂未返回下载地址，请继续查询")
                pending = out_path.with_suffix(".download.mp4")
                try:
                    with client.stream("GET", url) as download:
                        download.raise_for_status()
                        with pending.open("wb") as dest:
                            for chunk in download.iter_bytes():
                                dest.write(chunk)
                except httpx.HTTPError as exc:
                    raise PendingTask("成片已生成，下载中断；可继续下载原结果，不会重新生成") from exc
                media.deliver_video_for_browser(pending, out_path, duration)
                try:
                    media.check_video(out_path, duration)
                except Exception:
                    out_path.unlink(missing_ok=True)
                    raise
                return str(out_path)
            if state["status"] in {"FAILED", "CANCELED", "UNKNOWN"}:
                raise RuntimeError("口型任务失败：" + str(output.get("message") or state["status"]))
            time.sleep(5)
    raise PendingTask("口型任务仍在处理，可继续查询原任务")
