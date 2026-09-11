from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

from astrbot.api import logger

from .config import PluginConfig


@dataclass
class GSVRequestResult:
    ok: bool
    data: bytes | None = None
    error: str = ""
    text: str = ""
    file_path: str = ""

    @property
    def size(self) -> int:
        """音频数据大小（字节）"""
        return len(self.data) if self.data else 0

    @property
    def is_empty(self) -> bool:
        """是否无数据"""
        return self.size == 0

    def __bool__(self) -> bool:
        return self.ok and not self.is_empty



class GSVApiClient:
    """
    API 层（HTTP 通信）

    一个实例对应一个 GPT-SoVITS 服务地址，可同时存在多个
    """

    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.gpt_url = f"{self.base_url}/set_gpt_weights"
        self.sovits_url = f"{self.base_url}/set_sovits_weights"
        self.control_url = f"{self.base_url}/control"
        self.tts_url = f"{self.base_url}/tts"

        self.session = ClientSession(timeout=ClientTimeout(total=timeout))

    async def close(self):
        if self.session:
            await self.session.close()

    async def _request(
        self,
        url: str,
        *,
        params: dict | None = None,
    ) -> GSVRequestResult:
        request_text = ""
        if params:
            request_text = str(params.get("text", ""))
            params = {
                k: str(v).lower() if isinstance(v, bool) else v
                for k, v in params.items()
            }

        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    detail = await resp.text()
                    return GSVRequestResult(
                        ok=False,
                        error=f"HTTP {resp.status}: {detail}",
                        text=request_text,
                    )

                return GSVRequestResult(
                    ok=True,
                    data=await resp.read(),
                    text=request_text,
                )

        except ClientError as e:
            logger.error(f"[HTTP] 请求失败: {url} | {e}")
            return GSVRequestResult(False, error=str(e), text=request_text)

        except Exception as e:
            logger.exception(f"[HTTP] 未知异常: {url}")
            return GSVRequestResult(False, error=str(e), text=request_text)

    async def set_gpt_weights(self, path: str) -> GSVRequestResult:
        return await self._request(
            self.gpt_url,
            params={"weights_path": path},
        )

    async def set_sovits_weights(self, path: str) -> GSVRequestResult:
        return await self._request(
            self.sovits_url,
            params={"weights_path": path},
        )

    async def tts(self, params: dict) -> GSVRequestResult:
        return await self._request(
            self.tts_url,
            params=params,
        )

    async def restart(self) -> GSVRequestResult:
        return await self._request(
            self.control_url,
            params={"command": "restart"},
        )


class GSVClientPool:
    """
    按服务地址复用客户端，一个地址一个连接池

    每个 GPT-SoVITS 实例各自常驻一套音色，切换音色靠切换实例地址完成，
    因此这里不涉及热切换模型带来的阻塞。
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.timeout = config.client.timeout
        self._clients: dict[str, GSVApiClient] = {}

    def get(self, base_url: str | None = None) -> GSVApiClient:
        url = (base_url or self.cfg.client.base_url).strip().rstrip("/")
        client = self._clients.get(url)
        if client is None:
            client = GSVApiClient(url, timeout=self.timeout)
            self._clients[url] = client
            logger.debug(f"[HTTP] 新建客户端: {url}")
        return client

    async def close(self):
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
