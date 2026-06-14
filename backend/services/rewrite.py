"""步骤②改写文案：把爆款口播改写成"你的行业"版本（OpenAI 兼容大模型）。"""
from openai import OpenAI

SYSTEM = (
    "你是顶级的短视频口播文案改写专家。用户会给你一条爆款视频的原始口播文案，"
    "以及他所在的行业/产品。请把这条爆款的【结构、节奏、钩子、情绪】保留下来，"
    "但把内容彻底替换成用户行业的版本，做到：\n"
    "1) 开头3秒强钩子，精准圈定目标人群；\n"
    "2) 口语化、像真人讲话，适合数字人口播；\n"
    "3) 不照抄原文，不出现原视频里的具体品类词；\n"
    "4) 结尾有行动引导（点赞/关注/私信/到店等，按行业选其一）；\n"
    "5) 时长与原文相近，只输出改写后的纯文案，不要解释、不要分镜、不要标题。"
)


def rewrite_copy(transcript: str, industry: str, extra: str, cfg: dict) -> str:
    llm = cfg.get("llm", {})
    api_key = llm.get("api_key")
    base_url = llm.get("base_url")
    model = llm.get("model") or "deepseek-chat"
    if not api_key or not base_url:
        raise RuntimeError(
            "尚未配置大模型 API。请在【后台-设置】里填写 base_url / api_key / model。"
        )

    client = OpenAI(api_key=api_key, base_url=base_url)
    user_msg = (
        f"【我的行业/产品】{industry or '通用实体生意'}\n"
        f"【补充要求】{extra or '无'}\n\n"
        f"【原始爆款口播文案】\n{transcript}"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.8,
        max_tokens=1500,
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("大模型返回空文案")
    return text
