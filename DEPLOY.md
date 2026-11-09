# 一键追爆 SaaS · 部署上线清单（给别人试用）

目标：把整套放到一台**带 NVIDIA 显卡 + 公网 IP** 的云服务器上，
跑通「扒文案 → 改写 → 声音克隆配音 → 改口型出片」，别人打开网址就能试用。

---

## 一、租一台什么样的服务器

| 项 | 要求 | 说明 |
|---|---|---|
| 显卡 | **NVIDIA ≥ 8G 显存**（RTX 3060 12G / 4060Ti 16G / 4090 24G）| 改口型+声音克隆要它 |
| 内存 | **≥ 16G** | 你那台 2G 太小，必换 |
| 硬盘 | ≥ 60G | 放模型权重 |
| 系统 | Ubuntu 22.04 | 最省心 |
| 网络 | **公网 IP**（开放 80/443 端口）| 别人才能访问 |

**去哪租（按月或按量）**：
- 阿里云 / 腾讯云 GPU 云服务器（gn 系列）——正规、能开发票、有公网 IP，适合对外试用
- 便宜按量：AutoDL / 趋动云（开发测试便宜，但对外公开访问要确认端口）
- 包月约 ¥1000–2000，按小时更省（用完关机）

---

## 二、部署步骤（Ubuntu + NVIDIA 驱动已就绪）

```bash
# 0) 系统依赖
sudo apt update && sudo apt install -y python3-pip python3-venv ffmpeg git

# 1) 上传代码（本机 → 服务器）
#   本机执行：scp -r 一键追爆-SaaS user@服务器IP:/opt/ipboss

# 2) 后端依赖
cd /opt/ipboss/backend
pip3 install -r requirements.txt
pip3 install openai-whisper   # 扒链接转写

# 3) 改口型引擎
#   方案A（已带）：Wav2Lip —— engines/Wav2Lip 权重已在；服务器有 N 卡会自动用 cuda（更快）
#   方案B（更自然，推荐）：LatentSync —— 见 engines/README 安装，质量明显更好

# 4) 声音克隆引擎 CosyVoice2（独立 venv，避免和主环境冲突）
python3 -m venv /opt/cosyvoice-venv
/opt/cosyvoice-venv/bin/pip install cosyvoice   # 或按官方 repo 安装
#   下载 CosyVoice2-0.5B 权重；准备 infer_cosyvoice.py（接收 --ref --ref-text --text --out）
#   然后在 data/config.json 的 voice_clone 段填：
#   {"enabled":true,"python":"/opt/cosyvoice-venv/bin/python","script":".../infer_cosyvoice.py","model":".../CosyVoice2-0.5B"}

# 5) 配置大模型 key
cp backend/.env.example backend/.env   # 填 DeepSeek key（或登录后台填）

# 6) 起服务（当前任务执行器只支持单 worker）
cd /opt/ipboss/backend
pip3 install gunicorn
gunicorn app:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:80 --workers 1 --timeout 1800
```

打开 `http://服务器公网IP/` 即可对外试用。
（要用域名 + HTTPS：域名在国内需 ICP 备案，再用 Nginx 反代 + Let's Encrypt 证书。先用 IP 也能试。）

部署探针：`GET /health` 只检查进程和 SQLite 是否可用；`GET /ready` 还会检查改写模型和口型服务是否已配置，未就绪时返回 503。生产 HTTPS 要把 `COOKIE_SECURE=1`，并在反向代理层限制请求体大小、禁止把 `data/` 或 `outputs/` 目录配置成静态别名、配置访问日志脱敏。

---

## 三、上线前检查
- [ ] 改管理员默认密码（admin/admin888）
- [ ] 后台设置每条消耗点数、注册赠送点数
- [ ] 防火墙/安全组放行 80/443
- [ ] 给试用用户发：网址 + 注册即送点数
- [ ] 合规提示已在页面（用自有/授权素材）

---

## 四、并发与稳定（人多了再做）
- 当前后台线程执行器只适合单 worker 试用；正式售卖前必须换成持久化任务队列并做串行或多卡分发，避免多进程重复执行和抢显存
- Wav2Lip 的 temp/ 目录按任务隔离（多任务并发时）
- 大文件（成片）放对象存储 + CDN
