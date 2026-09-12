"""语音文本翻译

有些音色是用日语（或其它语言）训练的，让它用中文说话会有明显的外语口音，
听起来违和。开启「合成前翻译」后，机器人回复会先交给 LLM 翻译成该音色
要求的语言，再送去 GPT-SoVITS 合成。

注意：这里只影响**送去合成的文本**，机器人发出去的文字回复仍是原文。
"""

from __future__ import annotations

import re

from astrbot.api import logger
from astrbot.core.platform import AstrMessageEvent

from .config import PluginConfig, VoiceProfile

_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_LATIN = re.compile(r"[A-Za-z]")

LANG_NAMES = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
}

# 角色风格没填时的通用设定，避免译出来像机翻说明书
DEFAULT_STYLE = (
    "说话者是一位年轻女性，请使用自然、口语化的女性口吻，"
    "保留原文的语气、情绪、称呼习惯与亲疏关系。"
)


def detect_lang(text: str) -> str:
    """粗略判断文本语言，返回 zh / ja / ko / en，无法判断时返回空串

    只是个启发式判断，用于决定「要不要翻译」以及翻译失败时兜底的 text_lang，
    不需要很准。假名占比明显时才判定为日语，避免中文里的外来语被误判。
    """

    if not text:
        return ""

    kana = len(_KANA.findall(text))
    cjk = len(_CJK.findall(text))
    hangul = len(_HANGUL.findall(text))
    latin = len(_LATIN.findall(text))

    if kana >= 3 and kana >= (kana + cjk) * 0.25:
        return "ja"
    if hangul >= 2 and hangul >= cjk + kana:
        return "ko"
    if cjk >= 2 and cjk >= latin:
        return "zh"
    if latin >= 2:
        return "en"
    return ""


class VoiceTranslator:
    """调用 AstrBot 的 LLM Provider 做翻译

    失败一律返回 None，由调用方回退到原文，绝不让翻译失败把语音整条搞没。
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config

    async def translate(
        self,
        event: AstrMessageEvent,
        text: str,
        target_lang: str,
        profile: VoiceProfile | None = None,
    ) -> str | None:
        lang_name = LANG_NAMES.get(target_lang, target_lang)
        style = (profile.translate_style if profile else "") or DEFAULT_STYLE

        system_prompt = (
            f"你是一个翻译引擎。把用户给出的文本翻译成{lang_name}。\n"
            f"{style}\n"
            "要求：\n"
            "1. 只输出译文本身，不要输出原文、拼音、罗马音、解释，也不要加引号或「译文：」之类的前缀；\n"
            "2. 保留原文的情绪、语气强弱、称呼和颜文字 / emoji；\n"
            "3. 使用适合直接朗读出来的口语表达，不要写成书面语；\n"
            "4. 已经是目标语言的片段保持原样，不要改写。"
        )

        try:
            provider = self.cfg.get_translate_provider(event.unified_msg_origin)
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=text,
            )
            translated = self._clean(resp.completion_text)
            if not translated:
                raise ValueError("翻译结果为空")
            return translated
        except Exception as e:
            logger.error(f"翻译失败（目标语言 {target_lang}）: {e}")
            return None

    @staticmethod
    def _clean(text: str) -> str:
        """清理模型偶尔多输出的前缀与包裹引号"""

        result = (text or "").strip()
        result = re.sub(r"^(译文|翻译|日语|日本語|中文)\s*[:：]\s*", "", result)
        result = re.sub(r"^[\"'“”「『]+", "", result)
        result = re.sub(r"[\"'“”」』]+$", "", result)
        return result.strip()
