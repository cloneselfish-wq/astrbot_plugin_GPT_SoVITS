from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
from pathlib import Path, PureWindowsPath
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.provider import Provider
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)


class ConfigNode:
    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] miss key: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] is not dict"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() only support AstrBotConfig"
            )
        self._data.save_config()


class AutoConfig(ConfigNode):
    only_llm_result: bool
    tts_prob: float
    max_msg_len: int


class ClientConfig(ConfigNode):
    base_url: str
    timeout: int


class ModelConfig(ConfigNode):
    gpt_path: str
    sovits_path: str


class JudgeConfig(ConfigNode):
    enabled_llm: bool
    provider_id: str


class VoiceProfile(ConfigNode):
    """音色档案：按机器人（self_id）绑定一整套音色参数"""

    name: str
    self_id: str
    enabled: bool
    base_url: str
    gpt_path: str
    sovits_path: str
    ref_audio_path: str
    prompt_text: str
    prompt_lang: str
    text_lang: str
    speed_factor: float
    fragment_interval: float
    emotion_ref_audio: bool

    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.gpt_path = PluginConfig.normalize_path(self.gpt_path or "")
        self.sovits_path = PluginConfig.normalize_path(self.sovits_path or "")
        self.ref_audio_path = PluginConfig.normalize_path(self.ref_audio_path or "")

    @property
    def ids(self) -> set[str]:
        """支持在 self_id 里用逗号分隔多个 QQ 号"""

        raw = str(self.self_id or "").replace("，", ",")
        return {part.strip() for part in raw.split(",") if part.strip()}

    @property
    def endpoint(self) -> str:
        """该档案使用的 GPT-SoVITS 实例地址"""

        return (self.base_url or "").strip().rstrip("/")

    def to_params(self) -> dict[str, Any]:
        """只返回填写了的项，空值不覆盖默认参数"""

        params: dict[str, Any] = {}
        if self.ref_audio_path:
            params["ref_audio_path"] = self.ref_audio_path
        if self.prompt_text:
            params["prompt_text"] = self.prompt_text
        if self.prompt_lang:
            params["prompt_lang"] = self.prompt_lang
        if self.text_lang:
            params["text_lang"] = self.text_lang
        if self.speed_factor:
            params["speed_factor"] = self.speed_factor
        if self.fragment_interval:
            params["fragment_interval"] = self.fragment_interval
        return params


class CacheConfig(ConfigNode):
    enabled: bool
    expire_hours: int
    path: str


class PluginConfig(ConfigNode):
    enabled: bool
    auto: AutoConfig
    client: ClientConfig
    model: ModelConfig
    default_params: dict[str, Any]
    judge: JudgeConfig
    cache: CacheConfig
    entry_storage: list[dict[str, Any]]
    bot_voices: list[dict[str, Any]]

    _plugin_name: str = "astrbot_plugin_GPT_SoVITS"

    def __init__(self, cfg: AstrBotConfig, context: Context):
        super().__init__(cfg)
        self.context = context

        self.data_dir = Path(get_astrbot_plugin_data_path()) / self._plugin_name
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name

        self.init_voice_profiles()

        self.model.gpt_path = self.normalize_path(self.model.gpt_path)
        self.model.sovits_path = self.normalize_path(self.model.sovits_path)
        self.default_params["ref_audio_path"] = self.normalize_path(
            self.default_params["ref_audio_path"]
        )
        self.cache.path = self.normalize_path(self.cache.path)

        self.builtin_entry_file = self.plugin_dir / "builtin_entry.yaml"

        self.audio_dir = (
            Path(self.cache.path) if self.cache.path else self.data_dir / "audio"
        )
        self.audio_dir.mkdir(parents=True, exist_ok=True)

        self.save_config()

    def init_voice_profiles(self) -> None:
        """解析「音色档案」配置，过滤掉未启用或没填 self_id 的条目"""

        profiles: list[VoiceProfile] = []
        for item in self._data.get("bot_voices") or []:
            try:
                profile = VoiceProfile(item)
            except Exception as e:
                logger.error(f"[config] 音色档案解析失败: {e} | {item}")
                continue
            if not profile.enabled or not profile.ids:
                continue
            profiles.append(profile)

        object.__setattr__(self, "_profiles", profiles)

        if profiles:
            logger.info(
                "已加载音色档案："
                + "、".join(f"{p.name}({','.join(sorted(p.ids))})" for p in profiles)
            )

    @property
    def profiles(self) -> list[VoiceProfile]:
        return self.__dict__.get("_profiles") or []

    def match_profile(self, self_id: str | None) -> VoiceProfile | None:
        """按机器人 QQ 号匹配音色档案"""

        if not self_id:
            return None

        sid = str(self_id).strip()
        for profile in self.profiles:
            if sid in profile.ids:
                return profile
        return None

    @staticmethod
    def normalize_path(p: str) -> str:
        if not p:
            return p
        path_text = p.strip()
        if not path_text:
            return path_text

        match = re.search(r"([A-Za-z]:[\\/].*)$", path_text)
        if match and PureWindowsPath(match.group(1)).is_absolute():
            return match.group(1)

        if PureWindowsPath(path_text).is_absolute():
            return path_text

        path = Path(path_text).expanduser()
        if path.is_absolute():
            return str(path)
        return str(path.resolve())

    def get_judge_provider(self, umo: str | None = None) -> Provider:
        provider = self.context.get_provider_by_id(
            self.judge.provider_id
        ) or self.context.get_using_provider(umo)

        if not isinstance(provider, Provider):
            raise RuntimeError("未找到可用的 LLM Provider")

        return provider
