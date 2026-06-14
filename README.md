# 一键追爆 · 全自动 IP 智能体（SaaS）

把一条爆款短视频链接，自动变成「**换成你行业的文案 + 你的声音 + 你的脸对口型**」的新视频。
面向实体老板做短视频获客的一站式工具，可作为 SaaS 自营售卖（注册登录、按点数消费）。

> 灵感来自抖音上演示的「全自动 IP 智能体 / 一键追爆」类工具，本项目用开源能力把它复刻成可自部署、可白标的网页应用。

---

## ✨ 功能（6 步全自动流水线）

1. **文案生成** — 粘贴抖音爆款链接，自动下载并转写出原口播文案（本地 Whisper）
2. **改写文案** — 大模型把文案改写成你所在行业的版本（OpenAI 兼容，DeepSeek/豆包/通义…）
3. **音频生成** — 免费 TTS（edge-tts）或 **克隆你自己的声音**（CosyVoice）
4. **口播底版** — 上传你的正脸照片 / 人脸视频
5. **改口型成片** — 把底版的嘴型对到新配音上（Wav2Lip 本地免费 / LatentSync / 云端 API 可选）
6. **最终成片** — 在线预览、下载

附带：用户注册登录、点数计费、管理后台（配 key、管账号点数、改品牌）。

---

## 🧱 技术栈

- 后端：FastAPI + SQLite，零外部依赖即可起服务
- 前端：原生 HTML/CSS/JS，单页应用，深色橙色风
- AI 能力（均可插拔）：
  - 转写 `openai-whisper` ｜ 改写 OpenAI 兼容大模型 ｜ 配音 `edge-tts`
  - 改口型 `Wav2Lip`（自带）/ `LatentSync` / 腾讯·数智人等 API
  - 声音克隆 `CosyVoice2`

## 🚀 快速开始（本地）

```bash
cd backend
cp .env.example .env          # 填入大模型 key（也可登录后台再填）
pip install -r requirements.txt
pip install openai-whisper     # 扒链接转写
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

- 前台：http://127.0.0.1:8000/ ｜ 后台：http://127.0.0.1:8000/admin
- 默认管理员 `admin` / `admin888`（**上线前务必修改**）
- 改口型引擎（Wav2Lip/LatentSync）与声音克隆（CosyVoice）需单独安装，见 [DEPLOY.md](DEPLOY.md)

## ☁️ 部署上线

见 [DEPLOY.md](DEPLOY.md)：推荐一台 **NVIDIA ≥8G 显存 + ≥16G 内存** 的云 GPU 服务器，
按小时或包月，整条免费链路（改口型 + 声音克隆）跑在上面，零调用费。

## ⚙️ 配置

后台「设置」或 `backend/.env`：
- `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`：改写文案用的大模型
- `digital_human` / `voice_clone`：改口型与声音克隆引擎
- `credits_per_video / register_bonus`：计费

---

## ⚠️ 重要声明

- **不要提交任何密钥**：`.env`、`data/`（含 `config.json` 的 key 与用户库）、模型权重均已在 `.gitignore` 中排除。
- **合规使用**：本工具会"扒取他人爆款 + 生成配音 + 改口型"。请使用**自有或已授权的素材与声音**，遵守各平台规则；克隆名人声音/肖像、纯搬运他人内容有法律风险。
- **第三方许可**：改口型若用 `Wav2Lip`，其官方许可为非商业用途；商用请改用 Apache-2.0 的 `LatentSync` 或商用 API。
- 本仓库仅含应用代码；模型权重、引擎、用户数据均不入库，按 DEPLOY.md 自行安装。

## 📄 License

MIT（仅指本仓库的应用代码；所依赖的第三方模型/引擎以其各自许可为准）。
