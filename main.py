import base64
import random

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import Plain, Record
from astrbot.core.platform import AstrMessageEvent

from .core.client import GSVClientPool, GSVRequestResult
from .core.config import PluginConfig, VoiceProfile
from .core.emotion import EmotionJudger
from .core.entry import EntryManager
from .core.local_data import LocalDataManager
from .core.service import GPTSoVITSService


class GPTSoVITSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.local_data = LocalDataManager(self.cfg)
        self.entry_mgr = EntryManager(self.cfg)
        self.pool = GSVClientPool(self.cfg)
        self.judger = EmotionJudger(self.cfg)
        self.service = GPTSoVITSService(self.cfg, self.pool, self.local_data)

    async def initialize(self):
        if self.cfg.enabled:
            await self.service.load_model()

    async def terminate(self):
        await self.pool.close()

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        """取当前机器人的 QQ 号，用于匹配音色档案"""

        try:
            self_id = event.get_self_id()
        except Exception:
            self_id = getattr(event, "self_id", "")
        return str(self_id or "").strip()

    def _resolve_profile(self, event: AstrMessageEvent) -> VoiceProfile | None:
        return self.cfg.match_profile(self._self_id(event))

    @staticmethod
    def _to_record(res: GSVRequestResult, text: str | None = None) -> Record:
        if res.file_path:
            try:
                return Record.fromFileSystem(res.file_path, text=text)
            except Exception:
                logger.warning(f"无法读取文件：{res.file_path}, 已忽略")
                pass

        if not res.data:
            raise ValueError("无法获取结果数据")

        b64 = base64.urlsafe_b64encode(res.data).decode()
        return Record.fromBase64(b64, text=text)


    async def _get_emotion_params(
        self,
        event: AstrMessageEvent,
        text: str,
        profile: VoiceProfile | None = None,
    ) -> dict | None:
        entry = None

        if self.cfg.judge.enabled_llm:
            labels = self.entry_mgr.get_names()
            emotion = await self.judger.judge_emotion(event, text=text, labels=labels)
            if emotion:
                entry = self.entry_mgr.get_entry(emotion)

        if entry is None:
            entry = self.entry_mgr.match_entry(text)

        if entry is None:
            return None

        params = entry.to_params()

        # 音色档案默认锁死参考音频，情绪条目只影响语速与停顿，
        # 否则全局情绪条目的参考音频会把该机器人的音色覆盖掉
        if profile and not profile.emotion_ref_audio:
            for key in ("ref_audio_path", "prompt_text", "prompt_lang"):
                params.pop(key, None)

        return params

    @filter.on_decorating_result(priority=14)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送前钩子：把机器人即将发出的文本回复转成语音

        走的是「机器人自己的回复」这条路，而不是念外部传入的台词。
        消息链里的图片、@ 等非文本组件会原样保留；合成失败则退回原文，不会丢消息。
        """
        if not self.cfg.enabled:
            return

        cfg = self.cfg.auto
        result = event.get_result()
        if not result or not result.chain:
            return

        # 只处理 LLM 产生的结果（机器人的对话回复）
        if cfg.only_llm_result and not result.is_llm_result():
            return

        # 按概率决定是否转语音，tts_prob=1.0 表示每条都转
        if random.random() > cfg.tts_prob:
            return

        # 参与合成的文本片段（纯空白片段不参与）
        texts = [
            seg.text
            for seg in result.chain
            if isinstance(seg, Plain) and seg.text and seg.text.strip()
        ]
        if not texts:
            return

        combined_text = "\n".join(texts)

        # 文本太长就不转，直接原样发文字，避免合成耗时过长
        if cfg.max_msg_len and len(combined_text) > cfg.max_msg_len:
            logger.info(
                f"回复长度 {len(combined_text)} 字超过上限 {cfg.max_msg_len}，"
                "本次改为直接发送文字"
            )
            return

        profile = self._resolve_profile(event)
        params = await self._get_emotion_params(event, combined_text, profile)

        new_chain = []
        for seg in result.chain:
            if not isinstance(seg, Plain) or not seg.text or not seg.text.strip():
                # 非文本组件（图片、@、引用等）原样保留
                new_chain.append(seg)
                continue

            try:
                logger.info(
                    f"[{profile.name if profile else '默认音色'}] "
                    f"将回复转为语音（{len(seg.text)} 字）"
                )
                res = await self.service.inference(
                    seg.text, extra_params=params, profile=profile
                )
                if not bool(res):
                    logger.error(f"语音合成失败，改为发送原文: {res.error}")
                    new_chain.append(seg)
                    continue
                new_chain.append(self._to_record(res, text=seg.text))
            except Exception as e:
                logger.error(f"语音合成异常，改为发送原文: {e}")
                new_chain.append(seg)
                continue

            # 双输出：语音之外再保留一份文字
            if cfg.dual_output:
                new_chain.append(seg)

        if new_chain:
            result.chain = new_chain

    @filter.command("说", alias={"gsv", "GSV"})
    async def on_command(self, event: AstrMessageEvent):
        """说 <内容>, 直接调用GSV合成语音"""
        if not self.cfg.enabled:
            return

        text = event.message_str.partition(" ")[2]
        res = await self.service.inference(text, profile=self._resolve_profile(event))

        if not bool(res):
            yield event.plain_result(res.error)
            return

        yield event.chain_result([self._to_record(res)])

    @filter.command("重启GSV", alias={"重启gsv"})
    async def tts_control(self, event: AstrMessageEvent):
        """重启GPT_SoVITS"""
        if not self.cfg.enabled:
            return
        yield event.plain_result("重启TTS中...(报错信息请忽略，等待一会即可完成重启)")
        await self.service.restart()
        for profile in self.cfg.profiles:
            await self.service.restart(profile)

    @filter.llm_tool()
    async def gsv_tts(self, event: AstrMessageEvent, message: str = ""):
        """
        用语音输出要讲的话
        Args:
            message(string): 要讲的话
        """
        try:
            profile = self._resolve_profile(event)
            params = await self._get_emotion_params(event, message, profile)
            res = await self.service.inference(
                message, extra_params=params, profile=profile
            )
            if not bool(res):
                return res.error
            seg = self._to_record(res)
            await event.send(event.chain_result([seg]))
        except Exception as e:
            return str(e)
