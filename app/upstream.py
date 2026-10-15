"""抠图上游适配器：三模式（.env 的 UPSTREAM_MODE 切换）。

- tencent   腾讯云人像分割 SegmentPortraitPic（MVP 默认，0.01 元/次，月 1000 免费）
- oneapis   透传 oneapis 自定义渠道（二期：BiRefNet 挂进来后）
- selfhosted 自部署 rembg/BiRefNet 推理服务（自带 HTTP 接口）

注意：腾讯云接口必须判 Response.Error —— RequestId 存在不代表成功。
"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta

import httpx

from .config import settings


class UpstreamError(Exception):
    pass


class SegmentResult:
    def __init__(self, url: str, elapsed_ms: int = 0):
        self.url = url
        self.elapsed_ms = elapsed_ms


class CutoutUpstream:
    name = "base"

    def segment(self, image_url: str, output_format: str = "png") -> SegmentResult:
        raise NotImplementedError

    # ---- 本地文件转 base64（腾讯云取不到 BFF 内网 URL 时用） ----
    @staticmethod
    def _maybe_base64(image_url: str) -> dict:
        """http(s) 开头的公网 URL 直接透传；本地 /static/... 路径读文件转 base64。"""
        if image_url.startswith("http://") or image_url.startswith("https://"):
            return {"Url": image_url}
        # 本地文件：读 uploads 目录
        path = image_url.lstrip("/")
        if path.startswith("static/"):
            path = path[len("static/"):]
        abs_path = os_path_join(settings.UPLOAD_DIR, path)
        if not os.path.exists(abs_path):
            raise UpstreamError(f"local image not found: {abs_path}")
        with open(abs_path, "rb") as f:
            return {"ImageBase64": base64.b64encode(f.read()).decode("ascii")}


def os_path_join(*parts: str) -> str:
    import os

    return os.path.join(*parts)


class TencentSegmenter(CutoutUpstream):
    """腾讯云人脸识别（iai）人像分割 SegmentPortraitPic。

    计费：0.01 元/次（≤1 万次/月），每月 1000 次免费额度。
    开通：腾讯云控制台 → 人脸识别 → 人像分割。
    """

    name = "tencent"

    def _tc3_sign(self, host: str, action: str, payload: dict) -> dict:
        """TC3-HMAC-SHA256 签名（腾讯云 API 3.0 通用）。"""
        secret_id, secret_key = settings.TENCENT_SECRET_ID, settings.TENCENT_SECRET_KEY
        if not secret_id or not secret_key:
            raise UpstreamError("TENCENT_SECRET_ID/TC_SECRET_KEY not configured")

        service = "iai"
        version = "2020-03-03"
        region = settings.TENCENT_REGION
        algorithm = "TC3-HMAC-SHA256"
        timestamp = int(time.time())
        date = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

        # 1. CanonicalRequest
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_query_string = ""
        signed_headers = "content-type;host"
        payload_str = json.dumps(payload)
        hashed_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{host}\n"
        canonical_request = (
            f"{http_request_method}\n{canonical_uri}\n{canonical_query_string}\n"
            f"{canonical_headers}\n{signed_headers}\n{hashed_payload}"
        )

        # 2. StringToSign
        credential_scope = f"{date}/{service}/tc3_request"
        hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical}"

        # 3. 计算签名
        def _hmac(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

        secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
        secret_service = _hmac(secret_date, service)
        secret_signing = _hmac(secret_service, "tc3_request")
        signature = _hmac(secret_signing, string_to_sign).hex()

        authorization = (
            f"{algorithm} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Version": version,
            "X-TC-Region": region,
            "X-TC-Timestamp": str(timestamp),
        }

    def segment(self, image_url: str, output_format: str = "png") -> SegmentResult:
        host = "iai.tencentcloudapi.com"
        payload = self._maybe_base64(image_url)
        payload["RspImgType"] = "url"  # 返回结果图 URL（约 30 分钟有效，worker 成功后即用）
        headers = self._tc3_sign(host, "SegmentPortraitPic", payload)

        try:
            resp = httpx.post(
                f"https://{host}",
                headers=headers,
                content=json.dumps(payload),
                timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise UpstreamError(f"tencent network error: {e}")

        data = resp.json()
        # 关键：Response.Error 存在 = 业务失败（RequestId 存在不代表成功）
        if data.get("Response", {}).get("Error"):
            err = data["Response"]["Error"]
            raise UpstreamError(f"tencent api error: {err.get('Code')} {err.get('Message')}")

        result_img = data.get("Response", {}).get("ResultImage")
        if not result_img:
            raise UpstreamError(f"tencent empty result: {json.dumps(data)[:300]}")
        return SegmentResult(url=result_img)


class OneApiSegmenter(CutoutUpstream):
    """oneapis 透传：抠图模型挂到 oneapis 自定义渠道后用。

    请求/响应形态按实际渠道适配（默认假设 OpenAI images 风格，
    返回 data[0].url）；BFF 只维护这一处映射，换供应商不动小程序。
    """

    name = "oneapis"

    def segment(self, image_url: str, output_format: str = "png") -> SegmentResult:
        base = settings.UPSTREAM_BASE_URL.rstrip("/")
        url = f"{base}{settings.UPSTREAM_CUTOUT_PATH}"
        headers = {
            "Authorization": f"Bearer {settings.UPSTREAM_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": settings.UPSTREAM_CUTOUT_MODEL,
            "image": image_url,
            "response_format": "url",
        }
        try:
            resp = httpx.post(
                url, headers=headers, json=body, timeout=settings.UPSTREAM_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as e:
            raise UpstreamError(f"oneapis network error: {e}")
        if resp.status_code != 200:
            raise UpstreamError(f"oneapis http {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            result_url = data["data"][0]["url"]
        except (KeyError, IndexError, ValueError) as e:
            raise UpstreamError(f"oneapis bad response: {e}, body={resp.text[:200]}")
        return SegmentResult(url=result_url)


class SelfHostedSegmenter(CutoutUpstream):
    """自部署抠图服务（rembg serve / BiRefNet 推理服务）。

    默认假设服务接收 {"image_url": "..."} 返回 {"url": "..."}，
    按实际部署的推理服务微调 request/response 映射。
    """

    name = "selfhosted"

    def segment(self, image_url: str, output_format: str = "png") -> SegmentResult:
        url = f"{settings.UPSTREAM_BASE_URL.rstrip('/')}/segment"
        try:
            resp = httpx.post(
                url,
                json={"image_url": image_url, "output_format": output_format},
                timeout=settings.UPSTREAM_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise UpstreamError(f"selfhosted network error: {e}")
        if resp.status_code != 200:
            raise UpstreamError(f"selfhosted http {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
            result_url = data.get("url") or data.get("result_url")
        except ValueError as e:
            raise UpstreamError(f"selfhosted bad response: {e}")
        if not result_url:
            raise UpstreamError("selfhosted empty result url")
        return SegmentResult(url=result_url)


_UPSTREAMS = {
    "tencent": TencentSegmenter,
    "oneapis": OneApiSegmenter,
    "selfhosted": SelfHostedSegmenter,
}


def get_segmenter() -> CutoutUpstream:
    cls = _UPSTREAMS.get(settings.UPSTREAM_MODE)
    if not cls:
        raise UpstreamError(f"unknown UPSTREAM_MODE: {settings.UPSTREAM_MODE}")
    return cls()
