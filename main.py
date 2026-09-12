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
from .core.langswitch import LANG_ALIASES, LANG_CODES, LanguageGate, parse_lang_request
from .core.local_data import LocalDataManager
from .core.service import GPTSoVITSService
from .core.translate import LANG_NAMES, VoiceTranslator, detect_lang
from .core.voicerequest import parse_voice_request

# 「语音语言 默认」可以接受的写法
_RESET_WORDS = {"默认", "恢復", "恢复", "原样", "原樣", "auto", "reset", "-"}


class GPTSoVITSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.local_data = LocalDataManager(self.cfg)
        self.entry_mgr = EntryManager(self.cfg)
        self.pool = GSVClientPool(self.cfg)
        self.judger = EmotionJudger(self.cfg)
        self.translator = VoiceTranslator(self.cfg)
        self.service = GPTSoVITSService(self.cfg, self.pool, self.local_data)
        # 群友点名换语音语言后，按会话记住一小段时间
        self.lang_gate = LanguageGate(self.cfg.translate.switch_ttl_seconds)

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

    def _voice_allowed(self, profile: VoiceProfile | None) -> bool:
        """该机器人是否允许发语音

        没命中音色档案时默认回退到全局配置（旧行为）。打开
        `auto.only_configured_bots` 后改为直接放弃——否则没配音色的机器人会借用
        全局配置里的音色开口，听起来像是替别的角色说话。

        例外：一条档案都没配置时仍回退全局，否则整个插件都不会出声。
        """

        if profile is not None:
            return True
        if not self.cfg.auto.only_configured_bots:
            return True
        if not self.cfg.profiles:
            logger.warning(
                "已开启「只给配置了音色的机器人转语音」，但没有任何音色档案，本次回退全局配置"
            )
            return True
        return False

    @staticmethod
    def _session_key(event: AstrMessageEvent, self_id: str) -> str:
        """会话级语言偏好的键：同一个人格在不同群里互不干扰"""

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return f"{umo}|{self_id}"

    def _switchable(self, profile: VoiceProfile | None) -> bool:
        """该音色是否允许被点播语言

        只有开了「合成前翻译」的音色才谈得上换语言：它本来就是用外语训练的，
        换个语言顶多口音不同。中文音色被要求说日语只会得到一口怪腔调，所以不参与。
        """

        return bool(
            self.cfg.translate.can_switch
            and profile is not None
            and profile.translate_target
        )

    def _pick_lang(
        self,
        event: AstrMessageEvent,
        profile: VoiceProfile | None,
        explicit: str = "",
    ) -> str:
        """决定本次合成用什么语言

        :param explicit: 调用方明确指定的语言（LLM 工具参数 / 指令），优先级最高
        :return: 语言代码；返回空串表示沿用音色档案的默认语言
        """

        if profile is None:
            return ""

        want = str(explicit or "").strip().lower()
        if want:
            return want if (want in LANG_CODES and self._switchable(profile)) else ""

        if not self._switchable(profile):
            return ""

        key = self._session_key(event, self._self_id(event))
        remembered = self.lang_gate.get(key)
        if remembered:
            return remembered

        request = parse_lang_request(
            getattr(event, "message_str", "") or "", profile.translate_target
        )
        if request is None:
            return ""

        name = LANG_NAMES.get(request.lang, request.lang)
        if request.sticky and self.lang_gate.set(key, request.lang):
            logger.info(f"[{profile.name}] 群友要求改用{name}，本会话已记住")
        else:
            logger.info(f"[{profile.name}] 群友要求改用{name}（仅本次）")
        return request.lang

    @staticmethod
    def _speech_error(res: GSVRequestResult, prefix: str = "已放弃转语音") -> str:
        if res.unreachable:
            return (
                f"本机 GPT-SoVITS 服务未启动或不可达，{prefix}。"
                "请检查本机实例与隧道是否正常。"
            )
        return f"语音合成失败，{prefix}：{res.error}"

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

    async def _prepare_speech_text(
        self,
        event: AstrMessageEvent,
        text: str,
        profile: VoiceProfile | None,
        target_lang: str = "",
    ) -> tuple[str, str | None]:
        """按音色档案与本次语言意图决定送去合成的文本

        默认按档案自己的 `text_lang` 发音（开了「合成前翻译」时先把回复翻成该语言）。
        群友点名换语言、或模型自己指定语言时，`target_lang` 会临时覆盖档案默认语言。

        :param target_lang: 本次要用的语言，空串表示沿用档案默认
        :return: (合成用文本, 需要覆盖的 text_lang)，不需要覆盖时第二项为 None
        """

        if not text or profile is None:
            return text, None

        target = str(target_lang or "").strip().lower() or profile.translate_target
        if not target:
            return text, None

        source = detect_lang(text)
        # 语言无法判断（纯符号 / emoji / 数字）时按档案默认走
        if not source:
            return text, None

        # 已经是目标语言：不必翻译，但档案默认语言不同时要显式指出来
        if source == target:
            return text, (target if target != profile.text_lang else None)

        translated = await self.translator.translate(event, text, target, profile)
        if not translated or translated == text:
            logger.warning(
                f"[{profile.name}] 翻译为 {target} 失败，本次改用原文（{source}）合成"
            )
            # 回退时把 text_lang 也改回原文语言，否则口音会很怪
            return text, source

        logger.info(f"[{profile.name}] 语音文本已翻译 {source} -> {target}: {translated}")
        return translated, target

    def _synth_lang(self, lang_override: str, profile: VoiceProfile | None) -> str:
        """本次合成实际用的语言：本次覆盖 > 音色档案默认 > 全局默认"""

        lang = str(lang_override or "").strip().lower()
        if lang:
            return lang
        if profile is not None:
            return str(profile.text_lang or "").strip().lower()
        return str((self.cfg.default_params or {}).get("text_lang") or "").strip().lower()

    def _needs_zh_fallback(
        self,
        text: str,
        lang_override: str,
        profile: VoiceProfile | None,
    ) -> bool:
        """这段语音是不是「用外语念中文内容」

        群友大多听不懂日语，外语音色说出来的语音对他们等于没信息，
        所以要把送合成前的那份原文（`text`）跟在语音后面。

        两个条件都满足才附：语音语言不是中文，**且原文本身是中文**。
        后者是为了避免「人格本来就回了日语」时又附一遍日语原文。
        """

        if not text or not text.strip():
            return False

        lang = self._synth_lang(lang_override, profile)
        if not lang or lang.startswith("zh"):
            return False

        return detect_lang(text) == "zh"

    @filter.on_decorating_result(priority=14)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送前钩子：把机器人即将发出的文本回复转成语音

        走的是「机器人自己的回复」这条路，而不是念外部传入的台词。
        消息链里的图片、@ 等非文本组件会原样保留；合成失败则退回原文，不会丢消息。
        """
        if not self.cfg.enabled:
            return

        # 本轮已经由 gsv_tts 工具发过语音了，别再转一次（否则同一条回复会发两段语音）
        if event.get_extra("gsv_tts_sent"):
            logger.debug("本轮已通过 gsv_tts 工具发送语音，跳过自动转语音")
            return

        cfg = self.cfg.auto
        result = event.get_result()
        if not result or not result.chain:
            logger.debug("本次没有待发送的结果，跳过自动转语音")
            return

        # 只处理 LLM 产生的结果（机器人的对话回复）
        if cfg.only_llm_result and not result.is_llm_result():
            logger.debug("本次结果不是 LLM 回复，跳过自动转语音")
            return

        # 群友点名要语音时绕过概率：低概率抽签只负责「平时偶尔来一句」，
        # 「点名叫她说」必须有确定性通道，否则会出现「明明点名了却不吭声」。
        requested = (
            parse_voice_request(getattr(event, "message_str", "") or "")
            if cfg.voice_on_request
            else ""
        )

        # 按概率决定是否转语音，tts_prob=1.0 表示每条都转
        if not requested and random.random() > cfg.tts_prob:
            logger.debug(f"未命中自动转语音概率（tts_prob={cfg.tts_prob}），本次发文字")
            return

        if requested:
            logger.info(f"群友要求语音（命中「{requested}」），本次跳过概率直接转语音")

        # 参与合成的文本片段（纯空白片段不参与）
        texts = [
            seg.text
            for seg in result.chain
            if isinstance(seg, Plain) and seg.text and seg.text.strip()
        ]
        if not texts:
            logger.debug("本次回复没有可朗读的文本片段，跳过自动转语音")
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
        if not self._voice_allowed(profile):
            logger.info(
                f"机器人 {self._self_id(event)} 未配置音色档案，本次直接发送文字"
            )
            return

        params = await self._get_emotion_params(event, combined_text, profile)
        # 群友点名换语言时走这个；没被点名就是空串，按档案默认语言发音
        want_lang = self._pick_lang(event, profile)

        new_chain = []
        for seg in result.chain:
            if not isinstance(seg, Plain) or not seg.text or not seg.text.strip():
                # 非文本组件（图片、@、引用等）原样保留
                new_chain.append(seg)
                continue

            try:
                speech_text, lang_override = await self._prepare_speech_text(
                    event, seg.text, profile, want_lang
                )
                seg_params = dict(params) if params else {}
                if lang_override:
                    seg_params["text_lang"] = lang_override

                logger.info(
                    f"[{profile.name if profile else '默认音色'}] "
                    f"将回复转为语音（{len(speech_text)} 字"
                    f"{'，' + lang_override if lang_override else ''}）"
                )
                res = await self.service.inference(
                    speech_text, extra_params=seg_params, profile=profile
                )
                if not bool(res):
                    if res.unreachable:
                        logger.warning(f"语音服务不可用，本条改为发送文字：{res.error}")
                    else:
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
            elif cfg.zh_text_on_foreign_voice and self._needs_zh_fallback(
                seg.text, lang_override, profile
            ):
                # 外语语音群友听不懂，把中文原文接在语音后面
                name = profile.name if profile else "默认音色"
                logger.info(f"[{name}] 语音语言非中文（{self._synth_lang(lang_override, profile)}），已附上中文原文")
                new_chain.append(seg)

        if new_chain:
            result.chain = new_chain

    @filter.command("说", alias={"gsv", "GSV"})
    async def on_command(self, event: AstrMessageEvent):
        """说 <内容>, 直接调用GSV合成语音"""
        if not self.cfg.enabled:
            return

        text = event.message_str.partition(" ")[2]
        profile = self._resolve_profile(event)
        if not self._voice_allowed(profile):
            yield event.plain_result("当前机器人未配置音色档案，无法使用语音。")
            return

        speech_text, lang_override = await self._prepare_speech_text(
            event, text, profile, self._pick_lang(event, profile)
        )
        params = {"text_lang": lang_override} if lang_override else None
        res = await self.service.inference(
            speech_text, extra_params=params, profile=profile
        )

        if not bool(res):
            yield event.plain_result(self._speech_error(res))
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

    @filter.command("语音语言", alias={"tts_lang"})
    async def speech_lang(self, event: AstrMessageEvent):
        """语音语言 [中文|日语|英语|韩语|默认]，切换本会话的语音语言"""
        if not self.cfg.enabled:
            return

        profile = self._resolve_profile(event)
        if not self._voice_allowed(profile):
            yield event.plain_result("当前机器人未配置音色档案，无法切换语音语言。")
            return

        key = self._session_key(event, self._self_id(event))
        arg = event.message_str.partition(" ")[2].strip()

        if not arg:
            current = self.lang_gate.get(key) or profile.text_lang
            yield event.plain_result(
                f"当前语音语言：{LANG_NAMES.get(current, current)}。"
                "要改的话发「语音语言 中文」，发「语音语言 默认」可以恢复。"
            )
            return

        if arg in _RESET_WORDS or arg.lower() in _RESET_WORDS:
            self.lang_gate.clear(key)
            default_name = LANG_NAMES.get(profile.text_lang, profile.text_lang)
            yield event.plain_result(f"已恢复默认语音语言（{default_name}）。")
            return

        lang = LANG_ALIASES.get(arg) or LANG_ALIASES.get(arg.lower())
        if not lang:
            yield event.plain_result("用法：语音语言 中文 / 日语 / 英语 / 韩语 / 默认")
            return

        if not self._switchable(profile):
            yield event.plain_result(
                "当前音色没有开启「合成前翻译」，语言由音色本身决定，无法切换。"
            )
            return

        if not self.lang_gate.set(key, lang):
            yield event.plain_result(
                "插件配置里的「语音语言记忆时长」是 0，改不了。"
                "把它设成大于 0 的分钟数再试。"
            )
            return

        minutes = self.cfg.translate.switch_ttl_minutes
        yield event.plain_result(
            f"好，接下来用{LANG_NAMES.get(lang, lang)}说（{minutes} 分钟内有效）。"
        )

    @filter.llm_tool()
    async def gsv_tts(self, event: AstrMessageEvent, message: str = "", lang: str = ""):
        """把你要说的话用真人语音发出去，而不是发文字。

        以下情况应当调用本工具：
        - 用户明确想「听」你说话：说「用语音说」「语音回复我」「念一下」「唱一句」「说给我听」等；
        - 用户要求你发出某种声音 / 语气 / 情绪，而这些靠文字表达不出来；
        - 你自己觉得这句话用声音说出来效果更好（例如道别、撒娇、唱歌、念剧中的台词）。
        普通聊天、回答问题、需要贴链接或代码时不要调用，直接用文字回复即可。
        调用成功后不要在文字里重复同一句话，简短附和一下就好（也不要再发一遍语音）。

        Args:
            message(string): 要用语音说出的内容。必须是可直接朗读的口语短句，100 字以内，不要带 Markdown、链接或括号内的旁白说明。
            lang(string): 语音语言，可选。留空(默认)表示说这个音色本来的语言。只有当用户明确要求换语言时才填：zh=中文、ja=日语、en=英语、ko=韩语。例如用户说「用中文说」就填 zh，说「说日语」就填 ja。
        """
        try:
            profile = self._resolve_profile(event)
            if not self._voice_allowed(profile):
                return "当前机器人没有配置音色，无法发语音，请直接用文字回复。"

            text = str(message or "").strip()
            if not text:
                return "没有提供要朗读的内容，请把要说的话填进 message 参数。"

            limit = self.cfg.auto.max_msg_len
            if limit and len(text) > limit:
                return (
                    f"内容过长（{len(text)} 字，上限 {limit} 字），"
                    "请精简后再调用，或直接用文字回复。"
                )

            # 情绪匹配用原文（可能是中文），送合成的是翻译后的文本
            # 语言：模型自己指定 > 群友点名 > 音色默认
            want_lang = self._pick_lang(event, profile, lang)
            speech_text, lang_override = await self._prepare_speech_text(
                event, text, profile, want_lang
            )
            params = await self._get_emotion_params(event, text, profile)
            if lang_override:
                params = dict(params or {})
                params["text_lang"] = lang_override

            res = await self.service.inference(
                speech_text, extra_params=params, profile=profile
            )
            if not bool(res):
                return (
                    "语音合成失败，请改用文字把内容回复给用户，不要重复调用本工具。"
                    f"（原因：{res.error}）"
                )

            seg = self._to_record(res, text=text)
            await event.send(event.chain_result([seg]))
            # 标记一下，避免发送前钩子再把文字回复转成第二段语音
            event.set_extra("gsv_tts_sent", True)
            return "语音已发送，不必再用文字重复同一句话。"
        except Exception as e:
            logger.exception("gsv_tts 工具执行异常")
            return f"语音合成异常，请改用文字回复。（{e}）"
