"""Non-generating checks for the configured self-hosted service."""
import httpx


def check_server(base_url, label):
    if not base_url:
        raise RuntimeError(label + "未填写服务地址")
    try:
        response = httpx.get(base_url.rstrip("/") + "/health", timeout=3)
    except httpx.RequestError as exc:
        raise RuntimeError(label + "无法连接，请先启动对应服务或改用可用的 API") from exc
    if response.status_code != 200:
        raise RuntimeError(label + "健康检查未通过，请确认服务的 /health 接口")
