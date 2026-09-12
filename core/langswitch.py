"""按群友要求切换语音语言

日语（或其它外语）训练的音色，打开「合成前翻译」后会把中文回复先翻成日语再合成，
听起来才自然。但群友有时候就是想听中文，会点名要求「说中文」。

这里负责识别这类请求，原则是：

- **默认不动**：没被要求时，一律沿用音色档案自己的 ``text_lang``；
- **点名才切**：只有消息里出现明确的请求（说中文 / 用日语说 / 换成中文…）才改语言；
- **记住一小会儿**：命中后按会话记住 ``ttl`` 秒，免得群友每一句都要强调一遍。

识别分两档：``sticky``（会被记住）与 ``non-sticky``（只影响被点名的那一次）。
模糊的表达只影响当次，避免一句话被误判后整个会话都跟着变。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

# 语言名 → 语言代码，供指令解析（「语音语言 中文」）
LANG_ALIASES: dict[str, str] = {
    "zh": "zh",
    "中文": "zh",
    "汉语": "zh",
    "国语": "zh",
    "普通话": "zh",
    "华语": "zh",
    "ja": "ja",
    "日语": "ja",
    "日文": "ja",
    "日本語": "ja",
    "en": "en",
    "英语": "en",
    "英文": "en",
    "ko": "ko",
    "韩语": "ko",
    "韩文": "ko",
    "朝鲜语": "ko",
}

LANG_CODES = ("zh", "ja", "en", "ko")

# 请求「切回某个语言」时使用的反向语言，用于处理「别说日语」这类否定式
_OPPOSITE = {"ja": "zh", "zh": "ja", "en": "zh", "ko": "zh"}

# 语言词表：中文多加了「人话 / 鸟语」这类口语说法
_LANG_WORDS: dict[str, str] = {
    "zh": r"(?:中文|汉语|国语|普通话|华语|人话|鸟语)",
    "ja": r"(?:日语|日文|日本語)",
    "en": r"(?:英语|英文)",
    "ko": r"(?:韩语|韩文|朝鲜语)",
}

_NEG_PREFIX = r"(?:别|不要|不用|不准|不许|甭|莫|不必|别再|不要老)"
_SWITCH_VERB = r"(?:换成|切成|改成|调成|切换成|换成|转为|转成|改用|换回|切回|切到|换到)"

# 明确的请求句式：会被记住一段时间
_STRONG_TEMPLATES = (
    r"(?:说|讲|念|唱|来)(?:一?句|两句|两?个|点)?\s*{w}",  # 说中文 / 来句中文
    r"{sv}\s*{w}",  # 换成中文 / 切回日语
    r"(?:用|以)\s*{w}\s*(?:说|讲|念|唱|来|回复|回答|交流|聊天)",  # 用中文说
    r"{w}\s*(?:说|讲|念|唱|来|回复|回答)",  # 中文说 / 日语回复
    r"(?:能不能|可以|可否|能否|可不可以)\s*(?:用|讲|说)?\s*{w}",  # 可以说中文吗
    r"{w}\s*(?:模式|发音)",  # 中文模式
    r"(?:听不懂|听不明白|听不惯)\s*(?:日语|日文|外语|你说的话|你说啥)",  # 听不懂日语
)

# 模糊的表达：只影响当次
_WEAK_TEMPLATES = (
    r"(?:说|讲|念|唱|用|来|换|改|切|转)\s*{w}",  # 用中文 / 换中文
    r"{w}\s*(?:吧|呢|呀|啊|啦|呗)",  # 中文呗
)


@dataclass(frozen=True)
class LangRequest:
    """一次语言点播"""

    lang: str
    #: 是否值得记住（明确的请求才记）
    sticky: bool = True


_STRONG: list[tuple[str, re.Pattern[str]]] = []
_WEAK: list[tuple[str, re.Pattern[str]]] = []
_NEG: list[tuple[str, re.Pattern[str]]] = []

for _code, _word in _LANG_WORDS.items():
    for _tpl in _STRONG_TEMPLATES:
        _STRONG.append((_code, re.compile(_tpl.format(w=_word, sv=_SWITCH_VERB))))
    for _tpl in _WEAK_TEMPLATES:
        _WEAK.append((_code, re.compile(_tpl.format(w=_word, sv=_SWITCH_VERB))))
    _NEG.append(
        (
            _code,
            re.compile(
                _NEG_PREFIX
                + r"(?:再|总是|老是|一直)?(?:给我)?(?:说|讲|用|念)?\s*"
                + _word
            ),
        )
    )

#: 超过这个长度就不做语言识别——长文里出现「中文」多半不是在要求换语言
MAX_SCAN_LEN = 60


def parse_lang_request(text: str, default_lang: str) -> LangRequest | None:
    """从用户消息里识别语言点播

    :param text: 用户发来的消息
    :param default_lang: 该音色的默认发音语言（即档案的 ``text_lang``）
    :return: 需要切换时返回 :class:`LangRequest`，否则 None（表示不用管，按默认来）
    """

    if not text or not default_lang:
        return None

    content = text.strip()
    if not content or len(content) > MAX_SCAN_LEN:
        return None

    # 否定式优先：「别说中文」里含有「说中文」，必须先拦掉
    for code, pattern in _NEG:
        if not pattern.search(content):
            continue
        if code != default_lang:
            # 否定的是别的语言，等于让 ta 继续说默认语言，无需切换
            return None
        target = _OPPOSITE.get(default_lang)
        return LangRequest(target, True) if target else None

    for code, pattern in _STRONG:
        if code != default_lang and pattern.search(content):
            return LangRequest(code, True)

    for code, pattern in _WEAK:
        if code != default_lang and pattern.search(content):
            return LangRequest(code, False)

    return None


class LanguageGate:
    """会话级语音语言偏好

    内存存储即可——bot 重启后大家重新点一次就好，没必要落盘。
    键由调用方给（建议 ``会话标识 + 机器人 QQ``）。
    """

    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = max(0, int(ttl_seconds or 0))
        self._state: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> str:
        item = self._state.get(key)
        if not item:
            return ""
        lang, expire = item
        if self.ttl_seconds <= 0 or time.monotonic() >= expire:
            self._state.pop(key, None)
            return ""
        return lang

    def set(self, key: str, lang: str) -> bool:
        """记住该会话的语言，返回是否真的记住了（ttl 为 0 时不记）"""

        if self.ttl_seconds <= 0 or not lang:
            return False
        self._state[key] = (lang, time.monotonic() + self.ttl_seconds)
        return True

    def clear(self, key: str) -> None:
        self._state.pop(key, None)
