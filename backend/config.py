"""运行时配置：默认值 <- .env 环境变量 <- data/config.json（后台可改）。"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = ROOT_DIR / "data"
OUTPUTS_DIR = ROOT_DIR / "outputs"
FRONTEND_DIR = ROOT_DIR / "frontend"
FACES_DIR = DATA_DIR / "faces"   # 用户上传的口播底版（照片/视频）
VOICES_DIR = DATA_DIR / "voices"  # 用户上传的声音样本（用于克隆）
DATA_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
FACES_DIR.mkdir(exist_ok=True)
VOICES_DIR.mkdir(exist_ok=True)


def find_user_face(user_id):
    """返回该用户上传的口播底版文件路径，没有则 None。"""
    for f in FACES_DIR.glob(f"u{user_id}.*"):
        return str(f)
    return None


def find_user_voice(user_id):
    """返回该用户上传的声音样本路径，没有则 None。"""
    for f in VOICES_DIR.glob(f"u{user_id}.*"):
        return str(f)
    return None

CONFIG_FILE = DATA_DIR / "config.json"

# 抖音爆款拆解 skill 的备料脚本（扒链接+转写复用它）
SKILL_DIR = Path.home() / ".claude" / "skills" / "douyin-baokuan-fenxi"
DOUYIN_PREP = SKILL_DIR / "scripts" / "douyin_prep.py"

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


def _defaults():
    return {
        "brand": {
            "name": "一键追爆",
            "slogan": "全自动 IP 智能体 · 实体老板智能拓客一站式解决",
        },
        "credits_per_video": int(os.environ.get("CREDITS_PER_VIDEO", "10")),
        "register_bonus": int(os.environ.get("REGISTER_BONUS", "30")),
        "llm": {
            "base_url": os.environ.get("LLM_BASE_URL", ""),
            "api_key": os.environ.get("LLM_API_KEY", ""),
            "model": os.environ.get("LLM_MODEL", "deepseek-chat"),
        },
        "digital_human": {
            "provider": os.environ.get("DH_PROVIDER", "none"),
            "base_url": os.environ.get("DH_BASE_URL", ""),
            "api_key": os.environ.get("DH_API_KEY", ""),
        },
    }


def _deep_merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config():
    cfg = _defaults()
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
