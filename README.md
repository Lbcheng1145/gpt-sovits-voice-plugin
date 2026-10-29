# GPT-SoVITS 语音插件（优化版）

基于 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 的 MaiBot 文本转语音插件，服务跑在本机或另一台机器上都行。
支持多音色、按内容自动选语气、按语言切换模型；LLM 可自主调用，用户也能用 `/tts` 手动触发。

> 连远程服务时，`gpt_sovits.server` 填对方地址（如 `http://192.168.1.10:9880`），
> 各风格的 `refer_wav` 和模型权重要填**服务器上**的路径——插件不会去校验本地是否存在这个文件。

> MaiBot SDK 2.x（`MaiBotPlugin` + `@Tool` / `@Command` + Pydantic 配置）。

---

## ⚠️ 与原版互斥，请勿同时安装

| | 插件 ID |
|---|---|
| **本插件（优化版）** | `github.lbcheng1145.gpt-sovits-voice-plugin` |
| 原版 | `CGl123.gpt-sovits-voice-plugin` |

两者功能完全重叠，同时启用会出故障：

- **命令冲突**：都注册 `/tts`、`/voice`、`/gptsovits`
- **工具冲突**：都声明 `tts_voice`，LLM 无法确定用哪个
- **重复合成**：两个都响应同一条消息，会重复发语音

**切换步骤**：在插件管理中禁用/卸载已装的那个 → 安装另一个 → 重载插件。

`config.toml` 不能直接复用（本版新增 `auto_style`、`speed_factor`、`languages` 等字段），
请参照本仓库的 `config.example.toml` 重新配置。

---

## 功能

**合成与音色**

- 🎛️ **多音色**：每个风格独立配置参考音频、提示文本、模型权重
- 🧠 **自动语气**：未指定风格时，按文本内容（情绪词、标点）自动挑语气，规则可配置
- 🔢 **纯数字专用风格**：播报价格/时间/号码时可指定独立音色
- 🌏 **多语言路由**：检测文本语言，自动切到对应语言模型与参考音频
- 🔤 **底模版本匹配**：按底模版本自动匹配语言白名单，拦截不支持的语言
- ⏩ **语速控制**：`speed_factor` 全局调节
- ✂️ **首尾完整**：自动补句末标点；裁净弱起音后补 500ms 静音，避免首字被播放链路截掉
- 🎛️ **切分可调**：`text_split_method` / `min_chunk_length` / `overlap_length`

**触发方式**

- 🎙️ **LLM 自主触发**：planner 判断需要语音时调用 `tts_voice`
- 📝 **用户命令**：`/tts <文本> [-v <风格>]`
- 🎲 **概率语音**：按概率把普通文字回复自动转成语音
- 📞 **语音对讲**：对方发语音后开启通话窗口，窗口内回复都用语音

**健壮性**

- 🅰️ **@ 处理**：带 `at` 段的回复照常转语音，语音消息仍保留 @ 对象
- 🧹 **文本清洗**：剔除零宽字符、BOM 等不可见字符，避免合成失败
- 🛡️ **音频校验**：拦截静音或时长异常的合成结果
- 🧭 **配置校验**：缺失必要参数时给出明确修复指引
- ⏹️ **总开关**：`[plugin] enabled = false` 一键停用
- 🔌 **插件间接口**：`is_voice_active` / `set_voice_active` / `list_styles`

---

## 安装

把插件目录放进 MaiBot 的 `plugins/` 下（插件中心安装或手动放置均可）。
依赖 `aiohttp`，MaiBot 通常已自带。

### 首次配置（必做）

首次加载时 MaiBot 会依据配置模型生成 `config.toml`，其中 `refer_wav` 与
`prompt_text` 为空，**此时合成必然失败**。请：

1. 复制模板：`config.example.toml` → `config.toml`（含完整注释）
2. 至少填一个风格的 `refer_wav`（参考音频路径）与 `prompt_text`（该音频对应的文本）
3. 确认 GPT-SoVITS API 已启动（默认 `http://127.0.0.1:9880`）
4. 重载插件

> **参考音频要求**：3~10 秒、单句、干净无背景音乐的人声。
> `prompt_text` 必须与音频内容逐字对应，否则合成会出现杂音或空白。

配置不完整时，插件加载会列出缺少的参数，合成前也会提前拦截并说明缺哪一项，
而不是抛出服务端的原始错误。

---

## 配置

编辑插件目录下的 `config.toml`（完整注释见 `config.example.toml`）：

```toml
[plugin]
enabled = true                     # 总开关
config_version = "1.1.0"           # 配置结构版本，不可删除或留空

[general]
min_text_length = 2                # 文本最小长度，过短（如单字）拒绝合成
max_text_length = 200              # 文本最大长度，超过拒绝合成（超时风险）
use_base64_audio = false           # 务必保持 false，原因见下
audio_output_dir = ""              # 留空 = 用 Runner 分配的插件临时目录（推荐）
send_error_messages = true         # 合成失败时是否提示用户

[gpt_sovits]
server = "http://127.0.0.1:9880"
timeout = 60
model_version = "auto"             # 底模版本，决定允许合成哪些语言
speed_factor = 1.0                 # 语速倍率，越大越快（0.9~1.0 几乎无差别，1.2 以上才明显）

# 文本切分：决定句与句之间有没有停顿，是最影响自然度的参数
text_split_method = "cut0"         # cut0 整段合成（停顿自然）/ cut5 每标点都切（停顿明确）
min_chunk_length = 16              # 最小块长，块太短会导致开头起音不完整
overlap_length = 0                 # 交界处重复字数，非 0 会让某个字被念两遍

top_k = 15
top_p = 1.0
temperature = 1.0

# 自动语气（调用方未指定 style 时生效）
[gpt_sovits.auto_style]
enabled = true
fallback = "默认"                  # 需与下方某个 styles 的 name 完全一致
number_style = ""                  # 纯数字专用风格，留空不启用
number_style_include_chinese = true

# 每个风格是一套完整独立的参数
[[gpt_sovits.styles]]
name = "默认"
languages = ["zh"]                 # 可选：该风格适用的语言，留空表示通用
refer_wav = "/path/to/reference.wav"
prompt_text = "参考音频里说的那句话"
prompt_language = "zh"
gpt_weights = ""                   # 可选，动态切模型
sovits_weights = ""                # 可选

# 语言变体：命名规则 <风格名>_<语言>，前缀必须与已有风格名一致
[[gpt_sovits.styles]]
name = "默认_ja"
languages = ["ja"]
refer_wav = "/path/to/ja_reference.wav"
prompt_text = "日本語の参照テキスト"
prompt_language = "ja"
```

> **⚠️ 关于 `use_base64_audio`，请保持 `false`。**
>
> base64 模式会把整段音频编码后放进消息的 `raw_message`，MaiBot 会把它当普通
> 文本读入上下文并按 token 计费。一条十几秒的语音编码后约 30 万字符，单次请求
> 即可消耗数十万 token；且历史裁剪只按消息条数计算，这类超长消息不会被裁掉，
> 会在每轮请求中反复重发。
>
> v1.5.2 及更早版本默认 `true`，曾导致费用异常暴涨，v1.5.3 起改为 `false`。

### 自定义语气规则

内置规则已覆盖常见的兴奋、吐槽、撒娇特征。需要追加时：

```toml
[[gpt_sovits.auto_style.rules]]
style = "兴奋"                     # 命中后使用的风格名
pattern = "(抽到|出货|金了)"        # Python 正则
weight = 60                        # 命中权重，多条同时命中时累加后取最高
```

自定义规则**优先于**内置规则。写错正则会被跳过并记一条警告，不会影响其他规则。

---

## 使用

### 命令

```
/tts 你好世界                     # 自动选择语气
/tts 今天真开心 -v 兴奋            # 指定风格
/gptsovits 用本地语音合成          # 等价命令
/tts help                         # 查看帮助
```

### LLM 自主触发

planner 判断需要语音时会调用 `tts_voice`，传入 `text`（必填）与可选的 `style`。
未指定 `style` 时按文本内容自动选语气（规则由 `auto_style` 配置）。
显式指定的风格不存在时，回退顺序为：名为 `默认` 或 `default` 的风格 → 列表里第一个风格。

### 概率语音

让插件**自己按概率**把普通文字回复转成语音，更像真人随手发语音。

```toml
[random_voice]
enabled = true
probability = 0.3      # 约三成回复会变成语音
decision_ttl = 8.0     # 同一条回复 8 秒内复用同一结果
min_text_length = 2
max_text_length = 80
```

通过 `send_service.before_send` 钩子拦截出站消息，掷骰命中后把纯文本改写成语音。

| 行为 | 说明 |
|---|---|
| **一次回复只用一种形式** | 长回复常被拆成多段发送，按 `stream_id` 缓存结果 `decision_ttl` 秒，避免「前句语音、后句文字」 |
| **不适合的内容自动跳过** | 代码块、网址、表格、Markdown 列表/链接、HTML 标签一律不转 |
| **合成失败回退文字** | 语音合成出错时原样发文字，保证消息不丢 |
| **非纯文本消息不碰** | 带图片、文件、语音的消息不改写 |
| **不抢占引用** | 转语音时去掉引用（QQ 不支持「引用 + 语音」，带了会返回成功但不投递）|

与 LLM 工具调用的关系：两者独立、可同时开。LLM 主动调用的语音一定发；
概率语音只作用于 LLM 选择文字回复的场合。默认 `enabled = false`。

### 语音对讲

对方发来一条语音，就在这个会话里开启一段「通话」窗口（默认 3 分钟）。
窗口内她的每条回复都用语音发出，每条新语音都会续期；超时后自动恢复文字。

和概率语音不同，这里不是逐条掷骰子，而是**跟着对方的说话方式走**——
她发了语音，说明此刻方便听，那就一直用语音聊下去。

**与概率语音的区别**

| | 概率语音 | 语音对讲 |
|---|---|---|
| 触发依据 | 每条回复掷骰子 | 会话状态（对方发过语音）|
| 表现 | 偶尔冒一条语音 | 连续一段时间全用语音 |
| 适合 | 日常群聊、随手发语音 | 一对一深入聊、睡前夜谈 |

两者可同时开，**语音对讲优先**。

**工作方式**

```
对方发语音
    ↓
chat.receive.after_process 钩子
   发现语音 → 打开该会话的语音窗口（默认 180 秒）
   ★ 不 abort，消息正常入库、正常交给 Planner
    ↓
Planner 用完整人设与记忆生成回复
    ↓
send_service.before_send 钩子
   若会话在窗口内 → 把文字回复换成语音
   超长 / 含代码链接的回复保持文字
```

**为什么不用 abort**：入站钩子若打断消息链，消息就不会注册进 `chat_manager`、
也不会入库——对方的发言对她「不存在」，长期记忆和上下文都接不上，聊完就忘。
改为在发送前拦截后，记忆链路完整，回复也由 Planner 生成（带完整人设），
且不会双回复。

**配置**

```toml
[intercom]
enabled = true              # 总开关
private_only = true         # 只在私聊生效（默认开）
require_wake_word = false   # true = 先说唤醒词才开始
wake_words = ["语音对讲", "打电话", "对讲"]
min_text_length = 1         # 转写短于该字数不触发，防噪声误判
hold_seconds = 180          # 收到语音后多久内的回复都用语音（每条新语音续期）
max_reply_chars = 60        # 合成前的字数上限
oversize_strategy = "text"  # 超长时：text=保持文字 / truncate=截断后合成
```

**前提**：宿主必须开启 ASR，否则拿不到转写文本。

```toml
# config/bot_config.toml
[voice]
enable_asr = true
```

**注意**

- 语音窗口**只存在内存**，插件重载后会清空，需重新发语音进入
- 不影响入站消息持久化，对方的语音会正常写入聊天记录
- 群里发语音默认不触发（`private_only = true`）

### 对外 API

供其他插件调用（版本 `1`，全部公开）：

| API | 参数 | 用途 |
|---|---|---|
| `is_voice_active` | `stream_id` | 查询该会话现在是否会用语音回复 |
| `set_voice_active` | `stream_id`, `active`, `seconds` | 强制开关语音窗口 |
| `list_styles` | — | 列出可用音色名 |

```python
res = await self.ctx.api.call(
    "github.lbcheng1145.gpt-sovits-voice-plugin.set_voice_active",
    stream_id="4d12676e...",
    active=True,
    seconds=300,
)
```

---

## 工作原理

### 语气选择

调用方未指定 `style` 时，按以下顺序决策：

1. **纯数字判定**：文本只由数字构成时，若配置了 `number_style` 则直接使用
2. **规则匹配**：遍历自定义规则与内置规则，取权重最高者
3. **兜底**：无规则命中时使用 `auto_style.fallback`

#### 纯数字专用风格

播报价格、时间、电话号码时韵律与正常说话差异较大，同一音色往往效果不佳。

```toml
[gpt_sovits.auto_style]
number_style = "数字"                  # 留空则不启用
number_style_include_chinese = true    # 中文数字（一二三十百千）是否也算
```

| 视为纯数字 | 示例 |
|---|---|
| 阿拉伯数字 | `12345`、`3.14`、`-42`、`1,234.56` |
| 带单位 | `99元`、`2024年10月22日`、`12:30`、`50%`、`3号线` |
| 中文数字 | `一百二十三`、`九十`、`三千五百` |

| 不算纯数字（走正常流程） | 示例 |
|---|---|
| 中文夹数字 | `今天123`、`价格是99元` |
| 序数/量词 | `第3个`、`iPhone 15` |
| 纯符号 | `!!!`、`...` |

**优先级**：数字判断优先于自动语气规则。配了 `number_style` 后，`2333` 会走数字风格而非命中「兴奋」。

**注意**：`number_style` 不受 `auto_style.enabled` 影响，关掉自动语气后纯数字仍会用该风格。

### 语言路由

合成前检测语言，然后按四级优先级选风格：

| 优先级 | 规则 | 示例 |
|---|---|---|
| 1 | 当前风格本身支持该语言 | `zh` + `默认` → `默认` |
| 2 | 同名语言变体 `<风格>_<语言>` | `ja` + `默认` → `默认_ja` |
| 3 | 该语言的默认风格 | `ja` + `兴奋` → `默认_ja` |
| 4 | 保持原风格（无对应语言模型时）| `en` + `默认` → `默认` |

#### 语言检测规则

按顺序判定，命中即返回：

| 顺序 | 依据 | 结果 | 说明 |
|---|---|---|---|
| 1 | 出现**假名** | `ja` | 中文文本不会出现假名，最可靠 |
| 2 | 出现**韩文**（谚文）| `ko` | 韩语独有字符集 |
| 3 | 出现**粤语独占字** | `yue` | 如 唔 咁 咗 哋 佢 乜 嘢 嘅 嗰 嚟 冇 睇 喺 |
| 4 | 西里尔/泰文/阿拉伯文/天城文/希腊文 | `ru`/`th`/`ar`/`hi`/`el` | 按文字系统识别 |
| 5 | 纯拉丁字母 | `en` | |
| 6 | 汉字占比 > 30% | `zh` | |
| 7 | 无任何可识别字符 | `fallback`（默认 `zh`）| 纯数字/符号 |

**为什么假名优先**：中日共用汉字。若按字符比例统计，「今日はいい天気ですね」的汉字占比约 40%，
会被误判为中文。假名是中文绝不会出现的字符，所以作为第一判据。

**为什么粤语能单独识别**：粤语有大量普通话不用的独占字，命中即判定，误判率很低。
GPT-SoVITS 原生支持 `yue`。

### 语言支持范围（`model_version`）

不同底模支持的语言不同。插件据此决定「哪些语言能合成、哪些要拦截」，
避免把不支持的语言透传给后端，导致 400 报错或产出空白音频。

| `model_version` | 支持的语言 |
|---|---|
| `v1` | 中文 / 英语 / 日语 |
| `v2`、`v2Pro`、`v2ProPlus`、`v3`、`v4`、`v5dev`、`v5turbo` | 中文 / 英语 / 日语 / 粤语 |

**俄语、法语、德语、西班牙语、泰语、阿拉伯语、印地语、希腊语等，所有版本都不支持。**
**韩语（`ko`）在所有版本上都不可用**，并非版本差异，原因见下文。

`model_version = "auto"`（默认）时按权重路径自动推断，只看**文件名**与**直接父目录**：

```
SoVITS_weights_v2ProPlus/azuma3_e8_s368.pth   → v2ProPlus
SoVITS_weights_v1/azuma_e10.pth               → v1
weights/azuma_v3_e10.pth                      → v3
weights/my_model.pth                          → 推断不出，按 v2ProPlus 处理
```

之所以只看这两段，是因为 GPT-SoVITS 的**根目录**本身就叫 `GPT-SoVITS-v2pro-xxxx`。
若对整条路径匹配，一份 v1 权重会被根目录里的 `v2pro` 误判成 `v2Pro`。

选 `auto` 时按**每个风格自己的**权重路径分别推断，不同风格用不同版本也能各自识别：

```toml
[[gpt_sovits.styles]]
name = "默认"
sovits_weights = "SoVITS_weights_v2ProPlus/azuma3_e8_s368.pth"   # → v2ProPlus

[[gpt_sovits.styles]]
name = "旧模型"
sovits_weights = "SoVITS_weights_v1/azuma_e10.pth"               # → v1，按中/英/日处理
```

想统一指定、不做推断，把 `[gpt_sovits]` 的 `model_version` 填成具体版本即可。

#### 遇到不支持的语言时

分两种情况，既避免报错也避免产出垃圾音频：

| 情况 | 处理 | 例子 |
|---|---|---|
| 文本能用中文模型读出 | 回退 `zh` 合成，记 warning | 法语 `Bonjour` 这类拉丁字母文本 |
| 文本用中文模型读不出 | 跳过合成，返回失败 | 韩文谚文、俄文西里尔字母 |

之所以要区分：用中文音素硬读谚文或西里尔字母，API 虽不报错，但会返回一段
**极短的空白音频**（远低于插件 0.5 秒的有效性阈值），比直接失败更糟——
用户会以为插件坏了，却听不出原因。

> **关于韩语**：`ko` 能通过参数校验，但合成必然失败，返回
> `tts failed: exceptions must derive from BaseException`。
>
> 原因不在 GPT-SoVITS 版本，而在 Windows 下的韩语分词依赖：`text/korean.py`
> 需要 `eunjeon`（mecab 的韩语绑定）。没装时该模块会自动尝试
> `pip install eunjeon`，而它需要 MSVC 构建工具才能编译；安装失败后模块抛出
> 一个**并非 `Exception` 的对象**，于是 Python 报出那句看不懂的
> `exceptions must derive from BaseException`。
>
> v2/v3/v4/v5 共用同一份 `korean.py`，所以都会这样。本插件默认拦住，
> 避免用户面对一堆内部异常。
>
> 若你的机器已装好 `eunjeon`（能 `import eunjeon` 不报错），想让韩语透传：
> 编辑 `utils.py`，把 `SUPPORTED_LANGUAGES_BY_VERSION` 中对应版本集合里的 `ko`
> 加回去，再重载插件。

#### 已知局限

**纯汉字的日文无法自动区分**。例如人名「東雪蓮」，字符与中文完全相同，
没有技术手段能判断它属于哪种语言，会落入 `zh`。

遇到这类情况，在该风格配置中显式指定 `language`：

```toml
[[gpt_sovits.styles]]
name = "默认_ja"
languages = ["ja"]        # 声明支持的语言，供路由匹配
language = "ja"           # 强制合成语言，跳过自动检测
refer_wav = "..."
prompt_text = "..."
prompt_language = "ja"
```

两者区别：

- `languages`：**声明**该风格支持哪些语言，供语言路由挑选风格
- `language`：**强制**用指定语言合成，跳过自动检测

### 文本清洗

合成前自动剔除以下字符，避免服务端因编码问题返回 400：

- 零宽字符（`U+200B`–`U+200F`、`U+2060`–`U+206F`）
- BOM（`U+FEFF`）、软连字符（`U+00AD`）
- 方向标记（`U+202A`–`U+202E`）、C0/C1 控制符

**不做 @ 提及的字符级过滤**。出站正文里 `@` 出现率仅 0.14%，且几乎都是
说明性文字（如「先引用原话再@对方接内容」），按字符删会把语义直接删没。
真正的 @ 提及是独立消息段，插件按 `at` 段的昵称精确匹配剔除，
说明文字、邮箱、`user@host` 都不会被误伤。

---

## 常见问题

**合成返回 400，日志显示 GBK 编码错误**

GPT-SoVITS 服务端打印日语音素时会用到系统默认编码。启动服务时设 UTF-8：

```bash
# Windows
set PYTHONIOENCODING=utf-8
python -X utf8 api_v2.py -a 127.0.0.1 -p 9880

# Linux / macOS
PYTHONIOENCODING=utf-8 python api_v2.py -a 127.0.0.1 -p 9880
```

插件侧的文本清洗能挡住大部分情况，但服务端设 UTF-8 是更彻底的解法。

**日语合成发音不准**

插件只负责路由，发音质量取决于模型训练数据。用纯中文数据训练的模型合成日语，
只能依赖底模的跨语言能力，发音会有偏差。要准确需用日语语音数据单独训练一份，
再通过 `languages = ["ja"]` 声明。

**开头第一个字听不清**

插件已默认裁净弱起音并补 500ms 静音。若仍明显，可调大 `min_chunk_length`
（16 → 24~32），避免第一块太短导致起音来不及建立。

**结尾最后一个字含糊**

通常是参考音频本身收尾仓促——GPT-SoVITS 会用参考音频的韵律习惯去合成。
换一段自然收尾的参考音频效果最明显。

注意**不要**靠调慢 `speed_factor` 来解决：放慢会让音素边界更明显，
若参考音频本身收尾仓促，末尾字反而更含糊。

---

## 开发

### 测试

```bash
python tests/test_audio_payload.py
python tests/test_intercom.py
python tests/test_language.py
python tests/test_model_version.py
python tests/test_number_route.py
python tests/test_number_style.py
python tests/test_random_voice.py
python tests/test_units.py
```

共 **386 项**断言，覆盖语音载荷、对讲窗口、语言检测、底模版本、数字路由、
概率语音、文本清洗、音频校验、并发锁等。测试不依赖 `maibot_sdk`，
按文件路径直接加载被测模块。

### 模块结构

| 文件 | 职责 |
|---|---|
| `plugin.py` | 插件入口、工具与命令、合成链路编排、两条出站钩子 |
| `intercom.py` | 语音对讲窗口管理（按 stream 维护状态机）|
| `gpt_sovits.py` | HTTP 客户端、模型切换、风格解析与语言路由 |
| `style_router.py` | 语气规则引擎（正则匹配、权重排序、编译缓存）|
| `random_voice.py` | 概率语音（内容过滤、随机决策、结果缓存）|
| `utils.py` | 语言检测、文本清洗、音频校验、底模版本推断 |
| `audio.py` | 音频发送（base64 / 文件路径）与临时文件清理 |
| `config.py` | Pydantic 配置模型 |
| `config.example.toml` | 带完整注释的配置模板 |
| `assets/icon.svg` | 插件图标（矢量，WebUI 与插件中心使用）|

### 图标

图标在 `_manifest.json` 的 `display.icon` 声明，采用插件目录内的本地 SVG：

```json
"display": {
  "icon": {
    "type": "local",
    "value": "assets/icon.svg",
    "fallback": "mic"
  }
}
```

矢量图在不同尺寸下都清晰；`fallback` 是加载失败时退回的 Lucide 图标名。

### 发版

版本号在 `_manifest.json` 维护，tag 必须与之完全一致：

```bash
# 改 _manifest.json 的 version → 提交 → 打同名 tag
git tag v1.6.0 && git push origin v1.6.0
```

---

## 来源与许可

本项目基于 [CGl123/gpt-sovits-voice-plugin](https://github.com/CGl123/gpt-sovits-voice-plugin)
优化而来，原作者 **CGl123**（原始功能与整体设计均出自其手）。

本版新增/改进：自动语气、多语言路由、底模版本匹配、纯数字风格、概率语音、
语音对讲、语速控制、文本清洗、音频校验、配置校验、并发保护，
以及首字吞字、临时音频残留、总开关失效等问题的修复。

**本版本独立维护**，问题请反馈到[本仓库 Issues](https://github.com/Lbcheng1145/gpt-sovits-voice-plugin/issues)。
若不需要这些新增功能，建议直接使用原版。

[AGPL-3.0](LICENSE)
