#!/bin/bash
# 一键追爆 SaaS 启动脚本
cd "$(dirname "$0")/backend"

# 加载 .env（如有）
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

PORT="${PORT:-8000}"
echo "🔥 一键追爆 SaaS 启动中… http://127.0.0.1:$PORT"
echo "   前台: http://127.0.0.1:$PORT/    后台: http://127.0.0.1:$PORT/admin"
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT"
