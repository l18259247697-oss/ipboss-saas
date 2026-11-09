"""运行时配置：默认值 <- .env 环境变量 <- data/config.json（后台可改）。"""
import json
import os
from pathlib import Path
from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", ROOT_DIR / "data"))
OUTPUTS_DIR = Path(os.environ.get("APP_OUTPUTS_DIR", ROOT_DIR / "outputs"))
FRONTEND_DIR = ROOT_DIR / "frontend"
FACES_DIR = DATA_DIR / "faces"   # 用户上传的口播底版（照片/视频）
VOICES_DIR = DATA_DIR / "voices"  # 用户上传的声音样本（用于克隆）
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
FACES_DIR.mkdir(exist_ok=True)
VOICES_DIR.mkdir(exist_ok=True)


def find_user_face(user_id):
    """返回该用户上传的口播底版文件路径，没有则 None。"""
    return _find_media(FACES_DIR, user_id, {".jpg", ".jpeg", ".png", ".mp4", ".mov"})


def find_user_voice(user_id):
    """返回该用户上传的声音样本路径，没有则 None。"""
    return _find_media(VOICES_DIR, user_id, {".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov"})


def _find_media(folder, user_id, extensions):
    files = [p for p in folder.glob(f"u{user_id}.*")
             if p.is_file() and p.suffix.lower() in extensions]
    return str(max(files, key=lambda p: p.stat().st_mtime_ns)) if files else None

CONFIG_FILE = Path(os.environ.get("APP_CONFIG_FILE", DATA_DIR / "config.json"))

# 抖音爆款拆解 skill 的备料脚本（扒链接+转写复用它）
_prep_candidates = [Path.home() / root / "skills/douyin-baokuan-fenxi/scripts/douyin_prep.py"
                    for root in (".agents", ".claude", ".codex")]
DOUYIN_PREP = Path(os.environ["DOUYIN_PREP"]) if os.environ.get("DOUYIN_PREP") else next(
    (p for p in _prep_candidates if p.is_file()), _prep_candidates[0])

# 中文字体（Pillow 渲染占位成片/头像用）
CN_FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
CN_FONT_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"

# 免费配音音色（edge-tts），友好名 -> voice id
VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓·温柔女声"},
    {"id": "zh-CN-YunxiNeural", "name": "云希·阳光男声"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬·专业男声"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊·亲和女声"},
    {"id": "zh-CN-YunjianNeural", "name": "云健·浑厚男声"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北·东北女声"},
]


def _defaults(env=None):
    env = os.environ if env is None else env
    return {
        "brand": {
            "name": "一键追爆",
            "slogan": "全自动 IP 智能体 · 实体老板智能拓客一站式解决",
        },
        "credits_per_video": int(env.get("CREDITS_PER_VIDEO", "10")),
        "register_bonus": int(env.get("REGISTER_BONUS", "30")),
        "llm": {
            "base_url": env.get("LLM_BASE_URL", ""),
            "api_key": env.get("LLM_API_KEY", ""),
            "model": env.get("LLM_MODEL", "deepseek-flash"),
        },
        "digital_human": {
            "provider": env.get("DH_PROVIDER", "none"),
            "base_url": env.get("DH_BASE_URL", ""),
            "api_key": env.get("DH_API_KEY", ""),
        },
        "voice_clone": {
            "provider": env.get("VC_PROVIDER", "none"),
            "api_key": env.get("VC_API_KEY", ""),
            "dashscope_api_key": env.get("DASHSCOPE_API_KEY", ""),
            "model": env.get("VC_MODEL", "FunAudioLLM/CosyVoice2-0.5B"),
        },
        "lipsync": {"provider": env.get("LS_PROVIDER", "local"),
                    "api_key": env.get("DASHSCOPE_API_KEY", "")},
        "asr": {"provider": env.get("ASR_PROVIDER", "local"),
                "api_key": env.get("DASHSCOPE_API_KEY", ""),
                "model": env.get("ASR_MODEL", "qwen-audio-3.0-asr-flash")},
    }


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    env = {**dotenv_values(BASE_DIR / ".env"), **dotenv_values(BASE_DIR / ".env.local"),
           **os.environ}
    cfg = _defaults(env)
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            _deep_merge(cfg, saved)
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return cfg
