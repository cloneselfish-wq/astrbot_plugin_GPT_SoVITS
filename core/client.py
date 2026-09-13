import asyncio
import time
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
    # 服务连不上（没启动 / 隧道断了 / 连接超时），与「服务在但合成报错」区分开，
    # 便于上层决定是「静默退回文字」还是「提示用户」
    unreachable: bool = False

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

    # 连不上之后进入冷却的时长（秒）：冷却期内不再发起请求，直接判定不可用。
    # 否则「本机 TTS 没启动」时每条消息都要白等一次连接超时，而正常合成本来就要几十秒，
    # 用户很难察觉是服务没起还是单纯慢。
    DOWN_COOLDOWN = 60

    #: 中文语速约 4.5 字/秒，合成耗时约为音频时长的 2 倍（实测），
    #: 用它按文本长度估算一次合成大概要多久。
    CHARS_PER_SEC = 4.5
    SYNTH_FACTOR = 2.0
    #: 估算时额外留的余量（秒）
    TIMEOUT_MARGIN = 30
    #: 动态放宽后的硬顶（秒）：再长也不超过这个值，避免请求永远挂在那
    MAX_TIMEOUT = 600

    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.gpt_url = f"{self.base_url}/set_gpt_weights"
        self.sovits_url = f"{self.base_url}/set_sovits_weights"
        self.control_url = f"{self.base_url}/control"
        self.tts_url = f"{self.base_url}/tts"

        # 只给 total 的话，服务没监听时也要耗满整个 total（可能 180 秒）；
        # 单独限制建连阶段，快速判定不可达。合成本身耗时很长，total 仍保持宽松。
        self.session = ClientSession(
            timeout=ClientTimeout(total=timeout, connect=5, sock_connect=5)
        )
        self._down_until = 0.0
        self._down_reason = ""

    @property
    def is_down(self) -> bool:
        return time.monotonic() < self._down_until

    def mark_down(self, reason: str) -> None:
        self._down_until = time.monotonic() + self.DOWN_COOLDOWN
        self._down_reason = reason
        logger.warning(
            f"[HTTP] {self.base_url} 连不上，{self.DOWN_COOLDOWN}s 内跳过语音：{reason}"
        )

    def mark_ok(self) -> None:
        if self._down_until:
            logger.info(f"[HTTP] {self.base_url} 已恢复可用")
        self._down_until = 0.0
        self._down_reason = ""

    async def close(self):
        if self.session:
            await self.session.close()

    def _timeout_for(self, text: str) -> ClientTimeout:
        """按待合成文本的长度放宽这一次请求的超时。

        配置里的 `client.timeout` 是按「一句话」估的（默认 60 秒）。私聊不再
        限制长度后，长文本按配置值必然超时——所以这里按长度线性放大，
        配置值只当下限，短文本的行为完全不变。
        """

        total = float(self.timeout or 60)
        if text:
            estimate = len(text) / self.CHARS_PER_SEC * self.SYNTH_FACTOR
            total = max(
                total, min(estimate + self.TIMEOUT_MARGIN, float(self.MAX_TIMEOUT))
            )
        return ClientTimeout(total=total, connect=5, sock_connect=5)

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

        # 冷却期内直接放弃，不发起连接
        if self.is_down:
            remain = self._down_until - time.monotonic()
            return GSVRequestResult(
                ok=False,
                error=f"服务不可达（{self._down_reason}），{remain:.0f}s 后重试",
                text=request_text,
                unreachable=True,
            )

        try:
            async with self.session.get(
                url, params=params, timeout=self._timeout_for(request_text)
            ) as resp:
                # 有响应就说明服务活着（哪怕状态码不是 200）
                self.mark_ok()

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

        except (ClientError, asyncio.TimeoutError, TimeoutError, OSError) as e:
            # 连接被拒 / 隧道断了 / 建连超时 —— 归为「服务不可达」
            logger.error(f"[HTTP] 请求失败: {url} | {type(e).__name__}: {e}")
            self.mark_down(f"{type(e).__name__}: {e}")
            return GSVRequestResult(
                False, error=str(e), text=request_text, unreachable=True
            )

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
