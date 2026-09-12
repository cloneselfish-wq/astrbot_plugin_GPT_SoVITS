<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_GPT_SoVITS?name=astrbot_plugin_GPT_SoVITS&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_GPT_SoVITS

_GPT-SoVITS 对接插件（TTS）_

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-Zhalslar-blue)](https://github.com/Zhalslar)

</div>

---

## 1. 介绍

`astrbot_plugin_GPT_SoVITS` 用于把 AstrBot 文本输出转换成语音输出，底层调用 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 的 API。

支持三种调用方式：

1. 指令转语音：手动输入命令立即合成语音（念指定文本）。
2. **自动转语音：Bot 自己的对话回复（LLM 结果）在发送前自动转成语音**。默认每条回复都转。
3. 工具调用：LLM 工具调用时，GPT-SoVITS 会作为 LLM 工具的 TTS 接口。

此外还支持情绪参数切换（按关键词或 LLM 判别情绪），实现不同语气/语速的播报效果。

---

## 2. 安装

### 2.1 部署 GPT-SoVITS

请先完成 GPT-SoVITS 本体部署：

- 官方仓库：[RVC-Boss/GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)
- 参考指南：[GPT_SoVITS 指南](https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e)

### 2.2 安装 AstrBot 插件

在 AstrBot 插件市场搜索 `astrbot_plugin_GPT_SoVITS` 并安装。

---

## 3. 快速开始

### 3.1 启动 GPT-SoVITS API

Windows 示例（在 GPT-SoVITS 根目录新建 `start_api.bat`）：

```bat
runtime\python.exe api_v2.py
pause
```

或直接命令行启动：

```bash
python api_v2.py
# 或
python3 api_v2.py
```

### 3.2 在 AstrBot 面板配置插件

路径：`插件管理 -> astrbot_plugin_GPT_SoVITS -> 操作 -> 插件配置`

至少确认以下三项：

1. `client.base_url`：GPT-SoVITS API 地址，默认通常是 `http://127.0.0.1:9880`
2. `default_params.ref_audio_path`：参考音频路径（必填，建议先用插件默认值）
3. `enabled`：总开关打开

### 3.3 验证是否可用

在聊天中发送：

```text
说 你好，我是语音测试
```

若收到语音消息，说明链路已打通。

---

## 4. 命令与调用方式

| 命令 | 别名 | 说明 |
| ----- | ----- | ----- |
| `说 <文本>` | `gsv <文本>`、`GSV <文本>` | 手动触发 TTS。启用缓存时，同参数请求会优先复用本地音频 |
| 自动转语音（无命令） | - | Bot 回复阶段自动转语音。触发条件：插件启用、命中 `auto.tts_prob` 概率、回复文本长度不超过 `auto.max_msg_len`。回复中的图片、@、引用等非文本组件会原样保留；合成失败会自动退回发送原文 |
| 工具调用（无命令） | LLM Tool | `gsv_tts` 工具，由 bot 自行决定何时用语音说话 |
| `重启GSV` | `重启gsv` | 请求 GPT-SoVITS 执行重启 |

### 4.1 `gsv_tts` 工具（让 bot 自己决定说话）

模型在以下情况会调用该工具：用户说「用语音说 / 语音回复我 / 念一下 / 唱一句 / 说给我听」，
或者需要靠声音表达情绪、模型自己觉得这句话说出来更好。普通聊天不会调用。

工具内部的护栏：

- 内容超过 `auto.max_msg_len` 字 → 拒绝并提示模型精简，或改用文字；
- 当前机器人未命中音色档案且已开启 `only_configured_bots` → 拒绝；
- 合成失败 / 服务不可达 → 返回指引让模型改用文字回复，不重复调用；
- 成功发出语音后打标记，**发送前钩子会跳过这一轮**，避免同一句回复既发语音又转一遍语音。

### 4.2 服务不可用时的行为（优雅降级）

本机 GPT-SoVITS 没启动、隧道断了、或合成报错时，插件**一律放弃转语音、按原文发送文字**，
不会丢消息，也不会让用户干等：

| 场景 | 行为 |
| --- | --- |
| 本机实例没启动 / 隧道断开 | 建连阶段 5 秒内判定失败，本条消息直接发文字 |
| 连续连不上 | 该实例进入 **60 秒冷却**，冷却期内不再发起请求，消息照常发文字；服务恢复后自动解除 |
| 服务在但合成报错（如 400/500） | 本条发文字，日志报 error，不进入冷却 |
| 音频命中本地缓存 | 即使服务不可用也能正常发出（不发起请求） |
| `/说` 指令 | 明确提示「本机 GPT-SoVITS 服务未启动或不可达」 |

---

## 5. 情绪功能说明

插件支持两种情绪参数匹配方式：

1. 关键词匹配：当文本包含某个情绪条目的任一关键词时，使用该条目的语音参数。
2. LLM 判别：开启 `judge.enabled_llm` 后，先让 LLM 判断情绪，再映射到对应条目。

优先级：

1. 若开启 LLM 判别，则优先使用 LLM 结果；
2. 若 LLM 不可用或未匹配成功，则回退到关键词匹配。

首次加载时会自动导入内置情绪条目（如“温柔 / 开心 / 生气”）到 `entry_storage`。

---

## 6. 配置速查

### 6.1 基础配置

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `enabled` | 插件总开关 | 部署完成后开启 |
| `client.base_url` | GPT-SoVITS API 地址 | 常见为 `http://127.0.0.1:9880` |
| `client.timeout` | API 请求超时时间（秒） | 网络慢或长文本可适当调大 |
| `model.gpt_path` | GPT 权重路径（`.ckpt`） | 可空，空则使用 GPT-SoVITS 当前默认模型 |
| `model.sovits_path` | SoVITS 权重路径（`.pth`） | 可空，空则使用 GPT-SoVITS 当前默认模型 |

### 6.2 回复转语音配置（`auto`）

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `only_llm_result` | 只处理 LLM 生成的回复 | 建议 `true` |
| `tts_prob` | 回复转语音的概率 | `0 ~ 1`。默认 `0.1`（约一成回复转语音）；`1.0` 表示**每条都转**。合成是同步等待，比例过高会明显拖慢回复 |
| `max_msg_len` | 转语音的最大文本长度 | 默认 `150`。超过该值直接发文字。文字越长合成越慢（实测合成耗时约为音频时长的 2 倍），不建议设得过大。该值同时也是 `gsv_tts` 工具的单次朗读上限 |
| `dual_output` | 语音与文字同时发送 | 默认 `false`（只发语音）。开启后先发语音再发文字，方便没开声音时阅读；合成耗时不变 |
| `only_configured_bots` | 只给配置了音色的机器人转语音 | 默认 `false`。打开后未命中「音色档案」的机器人直接发文字，不会借用全局配置里的音色开口。一条档案都没配置时本项不生效 |

> 想让机器人「平时用文字、想说话时才说话」，就把 `tts_prob` 设小（例如 `0.1`），
> 让模型在识别到「用语音说 / 念一下 / 唱一句」这类意图时主动调用 `gsv_tts` 工具。

### 6.3 默认 TTS 参数（`default_params`）

这些参数会作为每次请求 `/tts` 的默认值：

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `text` | 默认合成文本 | 手动命令未传文本时会使用 |
| `text_lang` | 目标文本语言 | `zh/en/ja/ko` |
| `ref_audio_path` | 参考音频路径 | 必填，建议先用可用的 `wav` |
| `prompt_text` | 参考音频对应文本 | 建议与参考音频内容一致 |
| `prompt_lang` | 参考文本语言 | 与 `prompt_text` 保持一致 |
| `top_k` / `top_p` / `temperature` | 采样参数 | 控制随机性与稳定性 |
| `speed_factor` | 语速倍率 | `1.0` 为原速 |
| `fragment_interval` | 语句片段间隔（秒） | 值越小节奏越紧凑 |
| `media_type` | 输出音频格式 | `wav/mp3/ogg`（建议 `wav`） |
| `text_split_method` / `batch_size` / `parallel_infer` 等 | 长文本和性能相关参数 | 按显存与效果微调 |

### 6.4 情绪判别配置（`judge` + `entry_storage`）

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `judge.enabled_llm` | 是否启用 LLM 判别情绪 | 不开则仅走关键词匹配 |
| `judge.provider_id` | 用于情绪判别的模型提供商 ID | 留空时跟随当前会话模型 |
| `entry_storage[].name` | 情绪名称 | 建议唯一，便于识别 |
| `entry_storage[].keywords` | 触发关键词列表 | 文本包含任一关键词即命中 |
| `entry_storage[].ref_audio_path` | 该情绪使用的参考音频 | 可与默认参考音频不同 |
| `entry_storage[].prompt_text/prompt_lang` | 该参考音频对应文本和语言 | 建议准确填写 |
| `entry_storage[].speed_factor/fragment_interval` | 该情绪下语速与间隔 | 用于塑造语气差异 |

### 6.5 缓存配置（`cache`）

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `cache.enabled` | 是否启用参数级缓存 | 建议开启；同参数请求可直接复用本地缓存 |
| `cache.expire_hours` | 缓存过期时间（小时） | `0` 表示永不过期；大于 `0` 时按缓存文件修改时间判定 |
| `cache.path` | 三种调用方式共用保存目录 | 支持相对/绝对路径；留空默认 `data/plugins_data/astrbot_plugin_GPT_SoVITS/audio` |

### 6.6 音色档案（多机器人多音色）

当一个 AstrBot 同时接入多个机器人、且需要让它们用不同音色说话时，使用「音色档案」。

配置位置：`插件配置 -> 音色档案`，每一项对应一个机器人。

| 字段 | 说明 | 建议/取值 |
| --- | --- | --- |
| `name` | 音色名称 | 仅用于显示与日志 |
| `self_id` | 机器人 QQ 号 | 多个账号用英文逗号分隔 |
| `enabled` | 是否启用该档案 | 关闭后回退到全局配置 |
| `base_url` | 该音色对应的 GPT-SoVITS 实例地址 | 与其它音色指向不同端口 |
| `gpt_path` / `sovits_path` | 该音色的模型权重 | 必须是运行 GPT-SoVITS 那台机器上的路径 |
| `ref_audio_path` | 该音色的参考音频 | 同上，3~10 秒纯净人声最佳 |
| `prompt_text` / `prompt_lang` | 参考音频的文本与语言 | 参考音频是日文就填 `ja` |
| `text_lang` | 合成文本的语言 | 参考音频是日文也可以合成中文 |
| `speed_factor` / `fragment_interval` | 语速与片段间隔 | 按角色语感微调 |
| `emotion_ref_audio` | 是否允许情绪条目覆盖参考音频 | 默认 `false`，只让情绪影响语速与停顿 |
| `auto_translate` | 合成前翻译成 `text_lang` | 默认 `false`。日文等外语训练的音色建议打开 |
| `translate_style` | 翻译口吻提示（可选） | 例如「活泼的年轻女性口语」，留空用通用设定 |

注意事项：

1. **一个 GPT-SoVITS 实例同一时间只能常驻一套音色。** 多个音色需要在部署 GPT-SoVITS 的机器上启动多个实例（例如 9880、9881），再在档案里指向不同端口。共用一个实例会导致后加载的音色覆盖前者。
2. **`ref_audio_path` 等路径是给 GPT-SoVITS 所在的机器用的。** 如果 AstrBot 与 GPT-SoVITS 不在同一台机器上，必须填 GPT-SoVITS 那台机器上的绝对路径。
3. 未命中任何档案的机器人，继续使用「模型配置」与「TTS 默认参数」里的全局设置。
4. 开启 `emotion_ref_audio` 后，情绪条目自带的 `ref_audio_path` 会覆盖档案音色，通常只在「同一机器人需要多套情绪音色」时才需要打开。

#### 让外语音色说母语（语音翻译）

用日语训练的模型直接合成中文，会出现「日本人念中文」的口音，听起来很违和。此时可以打开档案里的 `auto_translate`：

1. 把该档案的「合成文本的语言」（`text_lang`）设为 `ja`。
2. 打开「合成前翻译成合成文本的语言」（`auto_translate`）。

之后每次要合成语音时，插件会先用 LLM 把回复翻译成日语，再送去 GPT-SoVITS。要点：

- **只翻译送去合成的文本**，机器人发出去的文字回复仍是中文。
- 回复本身已经是日语（假名占比明显）时跳过翻译，不会二次改写。
- 纯 emoji / 数字等无法判断语言的文本直接合成，不浪费一次 LLM 调用。
- 翻译失败（超时、模型报错）时自动回退：改用原文合成，并把 `text_lang` 改回原文语言，语音不会丢。
- 翻译会多一次 LLM 调用，建议在 `插件配置 -> 语音翻译配置 -> 翻译用的LLM提供商` 里指定一个快速稳定的小模型；留空则用当前默认提供商。
- 可以在 `translate_style` 里补充人设（例如「活泼的年轻女性口语」），让译文更贴合角色。

### 6.7 本地数据与缓存机制

插件通过 `LocalDataManager` 统一管理本地音频数据：

1. 本地音频文件名：`gsv_<参数哈希>.<ext>`。
2. 哈希由完整请求参数生成（排序后 JSON + SHA256 截断），确保“参数一致 -> 命中同一文件”。
3. 音频扩展名来自 `media_type`（仅支持 `wav/mp3/ogg`，异常值回退为 `wav`）。
4. 当 `cache.enabled=true` 时，请求前先查缓存；命中则直接发送本地缓存文件，未命中才请求 GPT-SoVITS。
5. 请求成功后会按同一参数规则写入本地目录，供后续直接复用。
6. `cache.expire_hours=0` 时缓存永不过期；大于 `0` 时，过期缓存会在读取时自动删除。

---

## 7. 常见问题与排查

### 7.1 提示“合成失败”

优先检查：

1. GPT-SoVITS API 是否已启动；
2. `client.base_url` 是否正确；
3. `default_params.ref_audio_path` 文件是否存在；
4. GPT-SoVITS 控制台是否有报错信息。

### 7.2 机器人回复没有变成语音

常见原因：

1. `auto.tts_prob` 被调低（默认 `1.0` 表示每条都转）；
2. 回复文本超过 `auto.max_msg_len`（默认 150 字），超长回复会直接发文字；
3. `only_llm_result=true` 且该消息不是 LLM 输出（如指令回复）；
4. 回复长度超过 `auto.max_msg_len` —— 日志会打印「回复长度 N 字超过上限」；
5. 该机器人未命中音色档案，且开启了 `auto.only_configured_bots` —— 日志会打印「未配置音色档案」；
6. 本轮已经由 `gsv_tts` 工具发过语音 —— 属正常行为，避免重复发两段语音；
7. GPT-SoVITS 实例未启动或参考音频路径不可达 —— 日志会打印
   「语音服务不可用，本条改为发送文字」或「语音合成失败，改为发送原文」。
   前者说明连不上（服务没起 / 隧道断），后者说明服务在但合成报错；
8. 服务刚被判定为不可用，正处于 60 秒冷却期 —— 日志会打印「连不上，60s 内跳过语音」。

> 图像、@、引用等非文本组件不会导致整条回复被跳过，它们会被原样保留，
> 只有其中的文本片段会被转成语音。
>
> 注意：本插件走的是 AstrBot 的 `on_decorating_result` 钩子。若同时开启了 AstrBot 自带的
> TTS（`provider_tts_settings.enable`），会出现重复转语音，请保持其关闭。

### 7.3 情绪没有切换

1. 若使用关键词模式，确认关键词确实出现在回复文本中；
2. 若使用 LLM 模式，确认 `judge.provider_id` 可用且返回格式正确；
3. 确认目标情绪条目名称存在于 `entry_storage`。

---

## 👥 贡献指南

- 🌟 Star 这个项目！（点右上角的星星，感谢支持！）
- 🐛 提交 Issue 报告问题
- 💡 提出新功能建议
- 🔧 提交 Pull Request 改进代码

## 📌 注意事项

- 本插件优先兼容 GPT-SoVITS 官方实现与常见整合包。若使用第三方魔改版本，请以其 API 实际行为为准。
- 想第一时间得到反馈的可以来作者的插件反馈群（QQ群）：460973561（不点star不给进）


## 🙏 致谢

[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)， 1 min voice data can also be used to train a good TTS model! (few shot voice cloning)
