"""数字人对口型适配器（可插拔）。
provider:
  none    -> 返回 None，由 compose 出占位成片
  tencent -> 腾讯云·智能数智人（视频免训练）：文字/音频 -> 对口型 mp4（国内·可开发票）
  custom  -> 通用模板（HeyGem 自建 / 其它服务商），改 _call_custom 即可

接入只改这一个文件 + 在 data/config.json 的 digital_human 段填好凭证，
前端/流水线/计费都不用动。"""
import hashlib
import hmac
import json
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def generate(audio_path, text, avatar_id, cfg, job_id, out_dir: Path):
    dh = cfg.get("digital_human", {})
    provider = (dh.get("provider") or "none").lower()
    if provider == "none":
        return None
    if provider == "tencent":
        return _call_tencent(audio_path, text, avatar_id, dh, job_id, out_dir)
    if provider in ("custom", "heygem"):
        if not dh.get("api_key"):
            return None
        return _call_custom(audio_path, text, avatar_id, dh, job_id, out_dir)
    return None


# ============ 腾讯云·智能数智人（视频免训练）============
# 文档：提交 https://cloud.tencent.com/document/product/1240/116222
#       进度 https://cloud.tencent.com/document/product/1240/81270
def _tc3_sign(secret_id, secret_key, service, host, action, region, version, payload, dh):
    """腾讯云 API 3.0 TC3-HMAC-SHA256 签名，返回 headers。"""
    algo = "TC3-HMAC-SHA256"
    ts = int(_time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    body = json.dumps(payload, ensure_ascii=False)

    # 1. 规范请求
    canonical = (
        "POST\n/\n\n"
        f"content-type:application/json; charset=utf-8\nhost:{host}\n\n"
        "content-type;host\n"
        + hashlib.sha256(body.encode("utf-8")).hexdigest()
    )
    # 2. 待签字符串
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = (
        f"{algo}\n{ts}\n{scope}\n"
        + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    # 3. 计算签名
    def _h(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
    secret_date = _h(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _h(secret_date, service)
    secret_signing = _h(secret_service, "tc3_request")
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    # 4. 组装 Authorization
    auth = (f"{algo} Credential={secret_id}/{scope}, "
            f"SignedHeaders=content-type;host, Signature={signature}")
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json; charset=utf-8",
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": version,
    }
    if region:
        headers["X-TC-Region"] = region
    return headers, body


def _tc_call(dh, action, payload):
    """调一次腾讯云接口，返回 Response 字段。"""
    secret_id = dh["secret_id"]; secret_key = dh["secret_key"]
    region = dh.get("region", "ap-guangzhou")
    service = dh.get("service", "ivh")
    version = dh.get("version", "2019-03-11")
    host = dh.get("endpoint", f"{service}.tencentcloudapi.com")
    headers, body = _tc3_sign(secret_id, secret_key, service, host, action,
                              region, version, payload, dh)
    with httpx.Client(timeout=60) as cli:
        r = cli.post(f"https://{host}", headers=headers, content=body.encode("utf-8"))
        r.raise_for_status()
        data = r.json().get("Response", {})
        if data.get("Error"):
            raise RuntimeError(f"腾讯云返回错误：{data['Error']}")
        return data


def _call_tencent(audio_path, text, avatar_id, dh, job_id, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    # 形象（参考视频）：优先用任务选的形象映射，否则用默认
    ref_video = dh.get("avatar_map", {}).get(avatar_id) or dh.get("ref_video_url")
    if not ref_video:
        raise RuntimeError("腾讯云数智人未配置数字人形象（ref_video_url 或 avatar_map）")

    driver = dh.get("driver", "Text")  # Text=用文本+音色 / OriginalVoice=用我们的配音URL
    payload = {"RefVideoUrl": ref_video, "DriverType": driver}
    if driver == "Text":
        payload["InputSsml"] = text[:2000]
        payload["SpeechParam"] = {
            "TimbreKey": dh.get("timbre_key", ""),
            "Speed": dh.get("speed", 0),
            "Volume": dh.get("volume", 5),
        }
    else:  # OriginalVoice：需要可公网访问的音频URL（SaaS 上线后成立）
        payload["InputAudioUrl"] = dh.get("audio_public_base", "").rstrip("/") + \
            f"/outputs/{job_id}/voice.mp3"

    # 1) 提交制作任务
    sub = _tc_call(dh, dh.get("action_submit", "VideoMakeNoTrain"), payload)
    task_id = sub.get("TaskId") or sub.get("TaskID")
    if not task_id:
        raise RuntimeError(f"腾讯云未返回 TaskId：{sub}")

    # 2) 轮询进度
    video_url = None
    for _ in range(180):  # 最多 ~15 分钟
        _time.sleep(5)
        q = _tc_call(dh, dh.get("action_query", "DescribeVideoMakeProgress"),
                     {"TaskId": task_id})
        status = q.get("Status") or q.get("StatusCode")
        if str(status) in ("3", "SUCCESS", "Success", "FINISH"):
            video_url = q.get("MadeVideoUrl") or q.get("VideoUrl") or q.get("ResultUrl")
            break
        if str(status) in ("4", "FAIL", "FAILED", "Error"):
            raise RuntimeError(f"腾讯云数智人制作失败：{q}")
    if not video_url:
        raise RuntimeError("腾讯云数智人任务超时未完成")

    # 3) 下载（腾讯云只保留7天）
    out = out_dir / "video.mp4"
    with httpx.Client(timeout=120) as cli:
        with cli.stream("GET", video_url) as resp:
            resp.raise_for_status()
            with open(out, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    return {"video_url": f"/outputs/{job_id}/final/video.mp4", "placeholder": False}


# ============ 通用模板（HeyGem 自建 / 其它）============
def _call_custom(audio_path, text, avatar_id, dh, job_id, out_dir: Path):
    base = dh["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {dh['api_key']}"}
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=60) as cli:
        r = cli.post(f"{base}/v1/lipsync", headers=headers,
                     json={"avatar_id": avatar_id, "text": text})
        r.raise_for_status()
        task_id = r.json().get("task_id") or r.json().get("id")
        video_url = None
        for _ in range(120):
            _time.sleep(5)
            q = cli.get(f"{base}/v1/lipsync/{task_id}", headers=headers)
            q.raise_for_status()
            data = q.json()
            if data.get("status") in ("succeeded", "done", "success"):
                video_url = data.get("video_url") or data.get("url")
                break
            if data.get("status") in ("failed", "error"):
                raise RuntimeError(f"数字人服务失败：{data}")
        if not video_url:
            raise RuntimeError("数字人任务超时")
        out = out_dir / "video.mp4"
        with cli.stream("GET", video_url) as resp:
            resp.raise_for_status()
            with open(out, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    return {"video_url": f"/outputs/{job_id}/final/video.mp4", "placeholder": False}
