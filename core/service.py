from typing import Any

from astrbot.api import logger

from .client import GSVApiClient, GSVClientPool, GSVRequestResult
from .config import PluginConfig, VoiceProfile
from .local_data import LocalDataManager


class GPTSoVITSService:
    def __init__(
        self,
        config: PluginConfig,
        pool: GSVClientPool,
        local_data: LocalDataManager,
    ):
        self.cfg = config
        self.default_params = config.default_params
        self.pool = pool
        self.local_data = local_data

    @staticmethod
    async def _load_one(
        client: GSVApiClient,
        gpt_path: str,
        sovits_path: str,
        tag: str,
    ) -> None:
        if gpt_path:
            result = await client.set_gpt_weights(gpt_path)
            if result.ok:
                logger.info(f"[{tag}] GPT 模型已加载: {gpt_path}")
            else:
                logger.error(f"[{tag}] GPT 模型加载失败: {result.error}")

        if sovits_path:
            result = await client.set_sovits_weights(sovits_path)
            if result.ok:
                logger.info(f"[{tag}] SoVITS 模型已加载: {sovits_path}")
            else:
                logger.error(f"[{tag}] SoVITS 模型加载失败: {result.error}")

    async def load_model(self):
        """加载默认音色，以及各音色档案对应的实例模型"""

        await self._load_one(
            self.pool.get(None),
            self.cfg.model.gpt_path,
            self.cfg.model.sovits_path,
            "默认音色",
        )

        loaded_endpoints: set[str] = set()
        for profile in self.cfg.profiles:
            endpoint = profile.endpoint or self.cfg.client.base_url.rstrip("/")
            if endpoint in loaded_endpoints:
                logger.warning(
                    f"[{profile.name}] 与其它音色档案共用实例 {endpoint}，"
                    "同一实例只能常驻一套音色，后加载的会覆盖前者"
                )
            loaded_endpoints.add(endpoint)

            await self._load_one(
                self.pool.get(profile.endpoint),
                profile.gpt_path or self.cfg.model.gpt_path,
                profile.sovits_path or self.cfg.model.sovits_path,
                profile.name,
            )

    async def inference(
        self,
        text: str,
        extra_params: dict[str, Any] | None = None,
        profile: VoiceProfile | None = None,
    ) -> GSVRequestResult:
        """TTS 推理"""

        params = self.default_params.copy()

        # 音色档案的优先级高于默认参数
        if profile:
            params.update(profile.to_params())

        if text:
            params["text"] = text

        if extra_params:
            filtered_params = {
                k: v for k, v in extra_params.items() if k in params
            }
            params.update(filtered_params)
            logger.debug(f"已更新已有参数: {filtered_params}")

        cached_audio = self.local_data.get_cached_audio(params)
        if cached_audio:
            cache_path, cached_data = cached_audio
            logger.debug("命中缓存，跳过 TTS 请求")
            return GSVRequestResult(
                ok=True,
                data=cached_data,
                text=str(params.get("text", "")),
                file_path=str(cache_path),
            )

        client = self.pool.get(profile.endpoint if profile else None)
        logger.debug(f"向 {client.base_url} 发起 TTS 请求，参数: {params}")
        result = await client.tts(params)

        if bool(result):
            cache_path = self.local_data.save_audio(result.data, params)
            if cache_path:
                result.file_path = str(cache_path)
        elif result.unreachable:
            # 服务没启动 / 隧道断了：上层会静默退回文字，这里不必刷 error 级日志
            logger.warning(f"TTS 服务不可达，跳过合成: {result.error}")
        else:
            logger.error(f"TTS 推理失败: {result.error}")

        return result

    async def restart(self, profile: VoiceProfile | None = None):
        client = self.pool.get(profile.endpoint if profile else None)
        result = await client.restart()
        if not result.ok:
            logger.error(f"重启失败: {result.error}")
