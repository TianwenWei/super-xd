# 浏览器 UI 验证记录

> 当前音频实现为真实 PCM 增量播放，最新测量与仲裁验证见文末“PCM 流式音频”。此前 MP3 记录仅保留为历史证据，其旧验收口径已被替代。当前唯一验收目标为 query 提交→浏览器首个含有效非零样本 PCM 包 ≤1.5秒（1500ms）；旧2秒口径属于历史要求，未覆盖当前目标。首字及车端回执仅作诊断，历史记录与原始测量保留不变。

2026-10-11，Europe/Paris。通过 CUA 的原生 Google Chrome 进行真实界面操作；IAB 在 subagent 中不支持 visibility，Chrome browser connector 又报 request-header policy 加载失败，因此使用 CUA 原生 app 路径。未使用固定模型响应或假联网结果。

## 已验证

- 100 functions / 10 skills 与十组中文可展开车况显示；空调执行后实际模拟电源开启、3 温区 23°C。
- `vehicle.command` 即时回发 `vehicle.execute`；只有 `vehicle.result` 到达才显示“执行成功”。结构化参数、回执及延迟可展开核对。
- 车窗开度变化后，关闭全部车窗实际使四窗均回到 0。
- professional/warm/playful 预设与结构化自定义“旅伴 / 平静亲切，表达简短”应用成功，session 未重建。
- 调研任务 `947ce23c…` 运行中追加“同时列出周一是否开放，结果用中文”，revision 1→2；从 playful 切 professional 后任务保持同一 ID；5 步完成，结果明确周一闭馆，附官方 `https://www.cite-sciences.fr/fr/infos-pratiques/horaires` 与真实查询时间 `2026-10-11T13:59:06.224816+00:00`，最终以专业风格“任务结果”播报。
- 后台任务进行时仍可聊天、调车、创建第二个任务、查进度和取消。
- 连续输入“出发前鼓励”及“打断一下，空调现在是多少度”，旧轮显示“已打断”，新轮独立回复；未取消已有后台任务。
- 用户要求固定火山 Speech xiaohe2.0 后，已移除浏览器 `speechSynthesis` 及音色选择。曾验证缺少 `VOLCENGINE_SPEECH_API_KEY` 时禁用朗读；后续服务端接入真实凭证，已真实播放小何2.0 MP3，见文末语音验证。
- 早期 UI 明示 mock 车控、导航模拟及当时的 P50 ≤2 秒目标；当前目标见文末更新。界面仍明确“长尾按分类加载 skill；检索本版未实现”。
- 修复 desktop flex 高度导致输入框掉到视口下方的问题；现输入框固定可见，三个区域独立滚动。用户消息气泡靠右，助手左对齐。
- 真实 Chrome DevTools 400×741 窄屏：对话/模拟车况/后台任务标签切换成功；连续多轮消息顺序正确，最新回复自动滚入可见范围，输入框始终可见，未观察到横向溢出。
- 后台主动结果的本地瞬时输出不再冒充用户提交到首字延迟；前端过滤 proactive 轮次的延迟展示。

## 真实单次延迟样本（不可代表 P50/P95）

数值为服务回传的该轮开始至有效文字/模拟执行回执；不是浏览器网络调度的独立计时。包含模型与网络实际耗时。

| 输入 | 首有效文字 ms | 车端回执 ms | 结果 |
| --- | ---: | ---: | --- |
| 把空调调到 23 度 | 1512 | 电源1476、温度1510 | 正确变更3温区 |
| 把左前车窗打开到一半 | 15480 | 15480 | **失败：旧prompt错误选择副驾**，已反馈主实现修复 |
| 把四个车窗都关上 | 6143 | 6143 | 四窗均0，延迟未达2秒 |
| 讲一个关于汽车和星星的长故事 | 1805 | 不适用 | 登记后台任务 |
| 你好，用一句话回答（后台运行） | 2944 | 不适用 | 正常闲聊，延迟未达2秒 |
| 打断一下，空调现在是多少度？ | 1563 | 无变更 | 正确回答23°C |
| 你好，我准备出发了 | 2738 | 2717、2718、2738 | **失败：旧prompt未经请求重复执行空调动作**，已反馈主实现修复 |
| 把左前车窗打开到一半（新会话回归） | 1938 | 1937 | **通过：主驾50%，副驾0**，自然反馈 |
| 你好，我准备出发了（新会话回归） | 2002 | 无新增 | 未改车；**失败：仍错误创建导航任务**，已反馈主实现 |
| 请给我一句简短的旅途祝福 | 1370 | 不适用 | 正常闲聊，用户右/助手左、顺序正确 |
| 你好，我准备出发了（最终13工具版本） | 1838 | 无新增 | **通过：任务0、回执0、未改车** |
| 把左前车窗打开到一半（最终版本） | 1334 | 1334 | **通过：主驾50%，副驾/后排0** |

这些样本既有 2 秒以内也有明显超过目标，不能据此宣称速度验收通过。正式分位数与完成率以项目评测报告为准。

## 截图

`validation/desktop-before-prompt-fix.png`：真实运行的桌面视图、车况、任务结果和延迟展示。

`validation/narrow-chat.png`：400×741 窄屏主驾车窗回归、用户右侧气泡与助手左侧回复。

`validation/narrow-streaming.png`：连续消息后的窄屏视图（截图时回复已完成），最新消息滚入视口，输入框可见。

`validation/narrow-vehicle.png`：窄屏车况标签与展开状态。

`validation/desktop-final.png`、`validation/narrow-final.png`：最终新会话寒暄不建任务/不改车、主驾50%、固定火山TTS缺凭证提示，以及用户右/助手左的桌面和400×741窄屏视图。

## 构建

`npm run build` 通过（TypeScript 严格检查 + Vite production build）；2026-10-11 安装69个总包，npm audit 0 vulnerabilities。

## 新会话回归与剩余限制

中间新会话 `a24a14b9…` 验证左舵映射修复；寒暄曾被误判为导航任务 `c90736d0…`。任务澄清回复“不需要导航，请结束这个任务”真实发送后 revision 1→2 并完成，未设置目的地。最终13工具版本新会话 `b522e23d…` 重测同一寒暄，任务与回执均保持0；随后左前车窗正确映射主驾，两个缺陷均已回归通过。

历史 MP3 实现使用 `audio.cancel` 与 AbortController；当前 PCM 实现使用 turn/playback ID 清队列与服务端取消，详见文末。客户端首包、开始播放与服务商首包/合成时间分别标注。首版只验收连续在线，不把新会话视为完整重启恢复支持。

## 真实火山 Speech 小何2.0验证

最终统一启动后，新会话 `275a91eb…` 在 CUA 原生 Chrome 勾选“火山朗读”。服务端固定 `volcengine-speech-tts / zh_female_xiaohe_uranus_bigtts`。观察到真实 MP3 进入浏览器播放，Chrome 标题和标签显示“正在播放音频”，UI 显示“正在朗读”，并具有独立语音计时。

下表均为真实单次样本，单位 ms。客户端下载首包从 HTTP 请求开始计时；服务商首包/合成来自独立响应头，不能相互替代，也不计入文字/车控速度。

| 朗读内容 | 文字首有效 | 客户端首包 | 开始播放 | 服务商首包 | 服务商合成 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 三句春天公园 | 1793 | 2629 | 2679 | 538 | 2621 |
| 四句雨后森林 | 2019 | 3814 | 3838 | 510 | 3807 |
| 收到 | 无有效文字指标 | 632 | 642 | 550 | 626 |
| 好的 | 无有效文字指标 | 769 | 796 | 656 | 763 |
| 六句秋天山林 | 1230 | 7054 | 7081 | 1572 | 7046 |
| 后台计算结果播报 | 后台主动轮不计首字 | 1624 | 1643 | 1086 | 1620 |

- 真实播放期间点击停止按钮，UI立即回到“准备就绪”，Chrome媒体控件停止；下一轮仍可朗读。
- 森林音频开始后发送“打断一下，只回答：收到”，旧MP3停止，新轮短句独立合成播放并自然结束。
- 海滩六句在“准备火山语音”期间立即发送“取消上一段朗读，只回答：好的”，旧轮没有下载/开播指标，也未迟到播放；新轮独立合成播放，无错误提示。
- 手动创建计算 `137×289` 的后台任务 `7642f786…`，随后发送六句山林描述；任务在山林语音合成等待期间已完成为39593，但其主动结果没有提前插入对话。
- 未发送新输入也未手动停止，山林MP3自然结束后，“查到的结果来了：39593”主动回流并通过火山合成播放；最终UI恢复“准备就绪”。

截图：`validation/speech-playing.png`、`validation/speech-task-waiting.png`、`validation/speech-task-released.png`、`validation/narrow-speech.png`。400×741窄屏复核四项语音指标自动换行，长段落可滚动，输入框常驻，无横向溢出。

当时的历史版本为完整MP3返回后播放，长段落实际开播超过7秒；已由文末的增量PCM实现替代。

## 模型输出 / Agent 轨迹抽屉（2026-10-11）

新增全局可见入口、每条助手消息的 turn 定位入口和每个任务卡的 task 定位入口。抽屉按会话/轮次/任务筛选，“模型原文”视图与完整事件时间线分开；模型 content/tool_calls/reasoning_content/usage/meta 及完整事件 JSON 可完整展开、复制。null、空字符串与缺失字段区分展示；不生成或补写模型思考。事件按 created_at、trace_id 排序，未知 kind 保留原名与完整 JSON。call_id/model_call_id 可筛选关联调用。

打开首次按200条分页读取完整会话，之后每秒使用 after_id 增量同步；关闭停止请求，缓存/游标保留。跟随最新默认关闭，增量事件不收起已展开项、不强制滚动。服务端 recording_error 单独警示记录可能不完整，读取失败自动重试且保留已加载记录。dialog 支持 Escape、可见控件焦点循环和关闭后恢复焦点。

真实 CUA Chrome（浏览器标签接口）新会话 `b0cb5edc9aed41f1`：

- 短聊天 `abe2e65c7c92`：“你好，请用一句话祝我旅途愉快。”实际回复“旅途愉快，一路顺利！”，首有效文字967ms；4条事件，模型 content 与最终反馈分区。Talker未返回 reasoning_content，明确显示未返回；实际 usage=null，未编造统计。复制当前4条JSON并读回解析成功，共18,616字符，含完整raw_chunks。
- 单次车控 `14281cd6177f`：左前车窗开到一半，实际参数 `set_window_position(driver,50)`；模型content=null，tool_calls、tool.started、vehicle.dispatched、vehicle.receipt、tool.completed与turn.completed均可展开。实际模拟回执2837ms；是小样本，未达到2秒目标。工具完整结果含车辆十组状态，未截断。点击model_call_id后可关联该模型开始/完成与工具事件。
- 手工任务 `b60ba8f436d4`：仅查官网周一开放，4步完成（Search×2、Extract、finish_task），中文结论主馆周一闭馆，并保留La Géode例外。任务卡直接定位38条任务事件；Thinker4轮均真实返回reasoning_content与usage，原文、模型名称、provider_id、finish_reason、elapsed_ms、raw_response可展开。Extract实际官网 `https://www.cite-sciences.fr/fr/infos-pratiques/horaires`，raw_content3041字符完整，带queried_at。复制模型视图8条JSON读回93,495字符；复制完整任务38条JSON读回535,142字符，均可JSON解析，Search/Extract/finish结果齐全。
- 读取旧聊天模型原文期间，后台增量由26条增长到42条；旧模型#3保持展开，scrollTop从0保持0，follow=false，未跳阅读位置。后续完成至全会话缓存50条，关闭再打开仍保留缓存。
- revision.started、step.started、task.progress等未预置kind在界面正常显示，并可展开完整事件JSON。
- 400×741响应式：抽屉全屏、范围选择/视图/复制/关闭可用；document与drawer宽度均400，展开的长JSON实际clientWidth=scrollWidth，无横向溢出。关闭回对话后全局入口、消息入口与输入框可见；已恢复桌面尺寸。

该次手工任务服务端曾误挂前一车控turn，已反馈并由根实现修复；上面的真实任务证据保留，不冒充修复后归属验证。原型服务重启不恢复会话；最终演示将重新新建。网络或recording_error故障文案由代码处理，本次真实运行未人为制造写入故障。

截图：`validation/trace-desktop.jpg`、`trace-thinker.jpg`、`trace-extract.jpg`、`trace-narrow.jpg`、`trace-narrow-json.jpg`、`trace-narrow-chat.jpg`。本次未发起TTS合成，原朗读验证记录保持。

最终重启后仅留简短演示会话 `1f027f0233104f87`：聊天 `25a85449…` 首有效1512ms，单次左前车窗 `9b5b08af…` 主驾50%，实际回执1371.63ms（UI1372ms）；手工算术任务 `f736f4b860d9` 一步完成39593。复制创建/运行/模型开始记录确认turn_id全为null，任务完成后worker相关事件仍全为null；主动回流有独立的新turn，不挂旧车控轮次。共26条可回看记录。刷新同标签后，snapshot恢复旧消息、结果与任务，旧聊天消息入口仍准确选中25a85449轮次。已恢复桌面，留全会话模型原文抽屉打开，真实Thinker reasoning与finish_task参数可见，截图 `validation/trace-final-ready.jpg`。

最终构建通过：TypeScript严格检查与Vite生产构建，30 modules，JS268.23kB/gzip85.05kB，CSS35.62kB/gzip8.61kB。抽屉事件和完整JSON缓存使用memo，空增量轮询不重新序列化大段记录。


## PCM 流式音频（当前版本，2026-10-11）

通过 CUA Chrome 浏览器标签接口执行真实表单提交与用户手势勾选。固定火山 xiaohe2.0；没有静态音频、浏览器语音或预录占位。独立音频 WS 传输32字节 playback ID + s16le / mono / 24kHz PCM，AudioWorklet 增量重采样至本机 AudioContext 的44.1kHz。模型输出在工具语义安全后交给语音；provider→浏览器为真实增量，不能宣称整个模型文本链已逐 token 合成。

核心口径是同一页面 performance.now 的“提交→首次收到含非零16bit样本的真实PCM包”。静音合法首包另记，metadata/空包/补零不计非零。入队仅是调度；Worklet真正消费首样本后通知主线程，记录通知观察值和contextTime/currentFrame。该值含线程通知延迟，不代表扬声器声学开播。

最终统一服务重启后新会话 `b45e4e2b88fe4763`：15条代表请求（7组×2，加负载闲聊1），再追加2条明确有意打断/仲裁验证请求。完整导出 `validation/pcm-final.json` 共17 query +3主动音频记录，所有慢样本和取消均保留；场景/phase/purpose见 `validation/pcm-final-manifest.json`。旧3次smoke独立保留 `validation/pcm-smoke.json`，未混入最终分布。首次启用到提交270.4ms，没有刻意等待；只将第1条称first-query-after-restart，其余warm-progressive-history。服务已模型预热，provider报告reused=true/prewarm_wait599ms，因此不声称模型/TLS/上游严格冷启动。首条提交时audio.connection尚未到达，后续收到真实连接诊断后保留其原文。

| 序号/场景 | 首非零 PCM ms | 首 render 通知 ms | 状态 |
| --- | ---: | ---: | --- |
| 1 / greeting | 1270.2 | 1282.7 | ended |
| 2 / greeting | 2672.6 | 2686.0 | ended |
| 3 / chat | 1549.8 | 1563.6 | ended |
| 4 / chat | 2940.5 | 2954.2 | ended |
| 5 / temperature | 2071.5 | 2103.3 | ended |
| 6 / temperature | 2134.6 | 2156.1 | ended |
| 7 / window | 2230.1 | 2260.5 | ended |
| 8 / window | 2073.2 | 2095.6 | ended |
| 9 / navigation | 2088.6 | 2110.1 | ended |
| 10 / navigation | 2116.4 | 2116.9 | ended |
| 11 / delegate | 1829.4 | 1837.5 | ended |
| 12 / delegate | 3847.0 | 3859.4 | ended |
| 13 / loaded_volume | 2436.4 | 2457.7 | ended |
| 14 / loaded_volume | 2346.9 | 2347.3 | ended |
| 15 / loaded_chat | 2882.4 | 2898.3 | ended |
| 16 / chat | 未收到 | 未渲染 | cancelled |
| 17 / chat | 3807.7 | 3825.2 | ended |

独立脚本统计全部17请求：首非零PCM16个有效观察，P50 2182.35ms、P95 3817.525ms；含有意取消的全部请求≤2秒3/17（17.65%）。明确的15条代表子集≤2秒3/15（20%）。这是小样本真实分布，**2秒目标没有整体达成**，不能仅凭第1条1270ms宣称验收通过。入队与非零render另计，完整统计以独立生成报告为准。

语义与时序：两次温度均真实3温区23°C、两次车窗均主驾50%、两次导航均完整“上海虹桥站”、两次负载音量均35%；每条音频均在同turn真实vehicle.result接收之后。首次天气委托task `269f6a3190ac` 登记1401.6ms、首非零PCM1829.4ms；第2次同文天气请求被模型路由manage_task查已有进度，不登记新任务，明确标注observed_route=task_progress，仍保留其3847ms速度失败。负载两次音量及秋天闲聊提交快照均有真实 `269f6a3190ac` 与 `3cbb17550c41` 两个running任务。

实际播放/打断/仲裁：

- 已render的天气主动音频 `f83c210bf205` 点击“停止朗读”后立即清队列并发server cancel，最终cancelled/user_stop；无自然drain伪记录。博物馆主动音频随后被新输入取消。
- 连续两次表单提交，旧春天轮 `8eafb6b4a669` 在首PCM前取消：bytes=0、无首帧/首render，后续没有迟到复播。新轮 `8f964134f49a` 独立ID播放，真实首非零3807.7ms，未因验证目的删除慢值。
- 新轮六句秋日公园音频播放时，手工算术任务 `5413b505` 已完成39593，任务卡显示完成，但对话未提前出现该结果。音频服务audio.ended在360599.5perf到达，队列继续真实消费至383690.7perf自然drain，才回报playback false。算术结果 `baee16c3b5b7` audio.started在383944.3perf（drain后253.6ms），最终自己自然ended。这验证input-ended不等于播放结束，后台播报等待真实队列耗尽。

400×741复核：页面clientWidth=scrollWidth=400，语音指标clientWidth=scrollWidth=288，长文本可滚，输入框在视口内；恢复默认桌面后全局模型输出/Agent轨迹面板仍可打开、完整记录持续同步。未再发起合成。

截图：`validation/pcm-smoke.jpg`、`pcm-final-desktop.jpg`、`pcm-final-narrow.jpg`、`pcm-task-completed-waiting.jpg`、`pcm-task-released.jpg`；最终可体验页会留桌面轨迹面板开启、火山朗读勾选。

验证：`npm run build --prefix frontend` 通过（TypeScript严格检查、Vite32 modules，JS279.65kB/gzip88.48kB，CSS36.61kB/gzip8.78kB）。`node --test frontend/tests/pcm-player-worklet.test.mjs` 4/4通过，覆盖44.1k/48k重采样时长、自然drain、空包/静音不冒充非零、clear后旧ID不能复入队。此前connection无ID空指针已修并build后reload，最终会话所有audio.connection消息正常。


## 口语优先与完整报告（跨两次部署，追加7条实际请求）

用户后续明确：不以关键词补洞、固定前N句或字数阈值机械删内容。按固定7 query计划执行；中途服务更新后未补跑前三条，只继续剩四条。所有失败/慢值保留，不能把跨部署的前三条当最新版本回归。

中间部署会话 `9d14ff134a3f4bc6` 三种人设同问候“你好，准备出发啦。”：warm43字、professional49字、playful46字。actual `audio.text` 都为model_content，原文/selected/spoken相等、未归一化；无车控、无任务，语义一致。措辞有差别，但实际仍偏解释型，不能声称已证明明显更短或人格区分强。完整原始文件 `validation/pcm-voice-first-intermediate.json`、`-intermediate-manifest.json`、`-intermediate-traces.json` 保留（41事件），阶段标注明确。

最终保守完整内容版本新会话 `ac97225626c04bfe` 仅剩4 query，默认warm；原始文件 `validation/pcm-voice-first.json` 与 `pcm-voice-first-manifest.json` 包含4请求+1主动音频，全部自然ended。

| 部署 / 输入 | 首非零 PCM ms | 首 render 通知 ms | 实际结果 |
| --- | ---: | ---: | --- |
| 中间 / warm问候 | 1903.4 | 1933.7 | 43字model_content，无动作 |
| 中间 / professional问候 | 1752.3 | 1765.8 | 49字model_content，无动作 |
| 中间 / playful问候 | 2418.6 | 2433.1 | 46字model_content，无动作，超过2秒 |
| 最新 / 所有温区23度 | 1836.9 | 1867.5 | 实际zone=all、3温区23；“空调调成二十三度了。” |
| 最新 / 音量999 | 2112.2 | 2125.3 | 模型直接拒绝，无commands，音量30未变；超过2秒 |
| 最新 / 完成拖很久的小事 | 4464.1 | 4478.8 | 实际短口语，超过2秒 |
| 最新 / 科学工业城官网核查 | 3454.1 | 3459.2 | 实际task登记3099ms，先于PCM；超过2秒 |

非法音量轮 `8b8cb2c00d2a` 的actual Speech文本为“音量范围是0到100，999超出了这个限制，没法设置哦。”，source=model_content，28字，0..100与schema一致。它没有触发validator反馈模板，不能将示例模板冒充实际朗读。温控 `faea73b18d4d` source=persona_template，10字；闲聊 `cb552a184301` actual文本“真棒呀，能把拖了很久的小事做完，肯定特别轻松吧～。”，原24/说25（增加句号），未丢实质内容。委托 `a3e60f612840` source=persona_template，43字，实际接手确认说明核查项目、保留官方来源与有结果告知。

真实官网任务 `94a2c1f7fa06` 5步完成，Search曾请求max_results10被schema上限5拒绝，随后真实搜索恢复并Extract官网 `https://www.cite-sciences.fr/fr/infos-pratiques/horaires`，读取时间 `2026-10-11T16:15:36.407689+00:00`。失败工具记录与后续恢复均保留，没有隐藏。

主对话最终主动轮 `acfbf186e2ac` actualaudio.text source=thinker_spoken_summary，original_chars678、selected_chars58、spoken_chars58、normalization_applied=false：

> 科学工业城主馆周一闭馆，周日开放时间是9点15分到19点。馆内展览区周日10点才开放，出行前可再以官网当天公告为准。

这是同一次Thinker finish_task生成的spoken_summary，完整678字summary并未机械截断。任务卡与完整trace仍保留主馆/展览区区别、固定闭馆日、LaGéode例外、官方原文、来源/读取时间、tool_observations。通过浏览器“复制当前记录JSON”复制115全事件、684653紧凑字符并成功解析，保存 `validation/pcm-voice-first-traces.json`；专用“复制实际合成文本”读回58字，保存 `pcm-voice-first-spoken.txt`。主对话短摘要与任务完整说明同时可见。主动结果从audio.started到首非零584.4ms，只作为主动音频诊断，不虚构query起点。

7条跨部署首非零≤2秒3/7（42.86%），最新4条1/4（25%）；均是小样本，2秒目标仍未达成。分位数/逐请求诊断由独立统计报告给出，不与phase1不同场景分布直接比较。所有“实际Speech文本”证据来自audio.text及浏览器PCM接收/Worklet观察，不宣称扬声器声学听审。

对照截图 `validation/pcm-before-long-research.jpg` 为phase1长篇天气结果主对话占据多屏；`pcm-voice-first-after-research.jpg` 为最新58字摘要主对话与完整官网报告任务卡并列；`pcm-voice-first-audio-text.jpg` 实际文本来源/678→58字可展开，`voice-first-intermediate-audio-text.jpg` 保留前三条中间版本证据。最终桌面留全会话轨迹抽屉打开、实际合成文本展开、火山朗读勾选；未继续外部合成请求。

最终前端构建通过32 modules，JS280.50kB/gzip88.65kB、CSS36.61kB/gzip8.78kB。纯本地 `node --test frontend/tests/*.test.mjs` 8/8通过：除Worklet4项外，覆盖无ID连接消息、音频先于控制turn.started、真实非零判定、新输入丢迟到start/PCM/render、自然drain才回报播放false。这些单元测试合成PCM与真实测量文件严格区分，未计入live分布。


## 当前预算更新：1500ms（仅本地界面更新）

当前唯一验收目标为 query 提交到浏览器首个含有效非零样本 PCM 包 ≤1500ms（1.5秒）。1.5秒是当前期望与预算目标，延迟未达到不阻塞本轮原型交付；实测超标继续如实保留。当前界面页脚、音频测量说明、主指标着色共用 `src/performanceTarget.ts` 的 `QUERY_TO_NONZERO_PCM_TARGET_MS`。只有首非零 PCM 主指标按此阈值着色：≤1500ms为达标色，>1500ms为超时色；缺失值保持中性。合法首包、入队、render观察、首字和车端回执仅作独立诊断，不另加2秒验收或着色。历史测量JSON、截图与实际数值未改。

用户给出的规划假设是 prefill 500ms、生成速度200 tokens/s、TTS首音400ms。这些是待验证的预算假设，不是本轮实测，也不代表网络、排队、生成与浏览器调度已被覆盖；不据此机械截断回复。

既有最终保守版本4个真实query首非零PCM依次为1836.9、2112.2、4464.1、3454.1ms；按当前目标为0/4（0%），全部未达1.5秒。主动结果无query提交起点，不能混入此分母。先前2秒口径的最新4条1/4、跨部署7条3/7，以及phase1全部17条3/17、代表子集15条3/15，均保留为历史统计，不当作当前验收通过。

本次未发起新query、TTS或浏览器操作，也未重启服务；只做本地构建与阈值边界检查。浏览器接收PCM与Worklet消费通知仍不代表扬声器声学起点。

恢复当前1500ms口径后本地构建通过：33 modules，JS280.97kB/gzip88.82kB、CSS36.69kB/gzip8.81kB。临时边界断言1500ms达标、1500.1ms超时、缺失/非有限/负数中性全部通过，并确认首字/回执没有验收阈值class。41个历史验证文件SHA-256仍全部一致。


## 火山 ASR 前端实现与本地验证（2026-10-12）

新增 `StreamingRecognition.ts`、`pcm-capture-worklet.js` 与小范围 App 语音接入及 `recognition.css`。麦克风按钮采用点击开始、再次点击结束并发送；识别预览独立于文字草稿，部分识别不调用 query。用户结束时 Worklet 先发送残余真实 PCM 并确认 flushed，释放采集设备后发送 `asr.stop`；仅匹配当前 capture、用户已结束且尚未消费过的 final 可以进入提交路径。小何 TTS 启用等待结束后再次校验 capture 与控制连接，防取消/新文字/断线期间旧识别提交。成功语音输入保留原 typed 草稿。

录音前经原控制 WS 发送独立 `asr.capture(active:true)`，随后暂停现有朗读；采集 gate 同时拒绝新后台音频，防止回录。取消/错误/断线/卸载清除本地采集与占位；权限请求晚返回时停止所有返回轨道。有效 final 在开启小何后发送原有 input，再释放占位，不以假 playback 状态实现仲裁。TTS 不可用时明确显示错误并保留文字提交，未使用浏览器音色替代。

新采集 Worklet 验证44.1k/48k连续重采样到16k、声道混合、s16le字节序与限幅、≤6400字节分包、flush不补虚假样本、静音输出和旧 capture 隔离。识别控制器验证部分识别全量替换、提前/重复/错ID final不提交、结束后只提交一次、空结果/错误/上传积压失败关闭、晚权限与断线释放设备、TTS异步启用期间取消不提交且不重新启用旧播放。原播放测试补充录音 gate 拒绝新的后台 PCM。

纯本地 `node --test frontend/tests/*.test.mjs` **30/30通过**；`npm run build --prefix frontend` TypeScript与Vite构建通过，36 modules，JS292.83kB/gzip92.27kB、CSS44.40kB/gzip10.14kB。测试使用隔离媒体设备、AudioContext与WebSocket，不访问硬件或外部识别/合成服务。根线程另行验证了云端ASR适配器；该结果不代替本轮尚未执行的真实浏览器麦克风、权限弹窗与端到端语音交互验证，也不作为声学听审证据。

导出 `recognition_records` 单独保存 ASR 阶段及全部失败/取消，成功提交可按 `client_request_id` 关联回复 PCM。ASR 不计入原 query-submit 起点；当前首非零回复 PCM 1500ms期望与所有历史实测文件保持原样。并行另一chat的布局、标题、车控 function-call 展示及兼容的测量导出改动已保留，不计为本次ASR实现。


## 真实ASR→query→PCM程序对照（2026-10-13，Pro run1）

最新交付约束：Pro速度不满足要求，不能交付。以下七条只保留为Pro对照，不计作最终Mini验收；Mini须在能力持久化修复版本使用同一固定oracle、复用fixture另存真实记录。该说明不修改以下实际数值或原始run1文件。

使用独立服务 `http://127.0.0.1:8001`、数据库 `data/acceptance-20261013.sqlite3`，全新会话 `b8c1d380b13341a7`。实际模型meta全部为 `doubao-seed-2-1-pro-260915`；用户8000服务未由此脚本修改。原始记录 `../docs/voice-interactions-run1.json` 保留七条请求、ASR部分/最终文本、控制事件、PCM包摘要、完整前后车况、模型/工具轨迹和后台完整结果，没有删除慢值或重试替换。

输入为真实小何合成中文fixture，转为16k/mono/s16le后按真实时长发送到 `/ws/{sid}/asr`；fixture原文与SHA保留于 `data/voice-interaction-fixtures`。七条最终转写仅容许标点及二十三/23差异，实际均匹配输入内容；只将云端唯一最终text经既有control input提交。该测试不访问实体麦克风、不操作用户录音按钮；清晰合成语音不能代表真人口音、噪声、回声、说话终点或权限弹窗体验。

| 场景 / turn_id | 实际行为与独立预期 | 提交→程序首非零PCM ms | ASR stop→程序final ms |
| --- | --- | ---: | ---: |
| 单车控 / `6342e7274213` | 仅all温控23，允许函数已声明的自动开启空调副作用 | 3331.6 | 253.6 |
| 复合 / `cffc0ca111e7` | 仅开空调、关全部窗、主驾按摩relax；无额外温度/风量/加热调用或变化 | 7407.7 | 313.4 |
| 状态问答 / `4b2d30d0851b` | “四扇车窗都处于全关状态”；无车控/任务调用 | 4084.0 | 254.0 |
| 否定更正 / `43c348c4d4ea` | 仅主驾按摩relax，无加热替代、无关闭动作 | 2662.5 | 371.0 |
| 闲聊 / `5442528895cf` | 无车控/任务；收到非零PCM后有意停止 | 3438.8 | 264.0 |
| 天气委托 / `0ba6718e913a` | 实际登记一个quick任务后才回接手音频 | 3775.3 | 218.6 |
| 任务中切话题 / `3e05fd50636a` | 回答“四扇车窗都是全关的”；无manage_task、无新任务或车控 | 2996.3 | 327.2 |

**语义/传输为7/7，延迟≤1500ms为0/7，两项独立。** 本表为Python单调时钟的WS接收值，ASR阶段另计；不是浏览器首帧指标，不能混入之前浏览器样本。程序按PCM样本数模拟队列自然耗尽并回报playback false，用于协议测试；没有实际AudioContext/Worklet消费，不宣称真实播放或声学出声。

每轮核对独立expected command集合与完整before/after状态差，未按模型回答反推gold。车窗答复要求全体关闭的正向事实，不接受“没关”“未关闭”“其他都关但主驾仍开着”或仅主驾关闭；同时检查实际四窗状态与无无关任务调用。独立oracle局部测试先发现“主驾车窗全关”可能误过，已收紧全体范围判定并补反例；仅对既有原始JSON做离线复核，仍7/7，无新增外部请求或修改run1文件。

闲聊播放 `335c75470d98420980370b017cc47858` 在首非零PCM后主动取消，trace128保存服务端取消；实际有一个19088字节迟到包，程序拒绝，未进入后续播放。七个query与一个后台主动结果使用八个不同playback ID，PCM与turn/request匹配，首音均在实际车端回执或任务登记后；没有把迟到数据、元信息或静音当有效首音。闲聊有意停止与后续录音打断委托接手均显式标记，没有冒充自然结束。

任务 `794e192e5fe0` 为quick，三步completed（trace184）；真实Search/Extract记录含法国气象局页面 `https://meteofrance.com/previsions-meteo-france/paris/75000`，读取时间 `2026-10-13T04:47:25.556766+00:00`。完整summary、来源、tool_observations与独立spoken_summary均保留；主动轮 `0c8751b754df` 实际合成文本在trace188、独立playback `9c4cb404147143d89ded872522d27ca5`，585462字节非零PCM完成程序模拟drain。脚本验证真实来源读取和结果结构，天气数值并非另一次人工事实核查。

本轮仅新增验收脚本/独立oracle测试与文档，未修改App、布局、session或worker。`node --test frontend/tests/*.test.mjs` 30/30通过；`pytest -q tests/test_voice_interactions.py` 18/18通过；当前TypeScript/Vite构建通过36 modules，JS292.83kB/gzip92.27kB、CSS44.40kB/gzip10.14kB。1500ms期望目标与旧浏览器原始记录保持不变，真实麦克风交互和浏览器实体播放仍未由本轮程序测试覆盖。


## Mini真实语音首批（2026-10-13，mini-run1，整体未通过）

原始 `../docs/voice-interactions-mini-run1.json` 使用 `http://127.0.0.1:8003`、独立数据库 `data/acceptance-mini-20261013.sqlite3`，新会话 `1dbd0089f1cb4938`。执行前 `/api/config` 与 `--expected-talker doubao-seed-2-0-mini-260428` 匹配；完整trace中Talker和quick Thinker实际model均为Mini，未用Pro前台。七条输入fixture全部复用，未重复生成输入TTS，query/gold与收紧后的Pro离线对照一致。

| 场景 / turn_id | 提交→程序首非零PCM ms | ASR stop→程序final ms | 当前轮实际检查 |
| --- | ---: | ---: | --- |
| 单车控 / `19e34af6d0da` | 1572.4 | 373.5 | 仅all温控23 |
| 复合 / `f58215052c9b` | 2617.6 | 312.7 | 仅开空调、全窗关闭、主驾massage=relax |
| 状态问答 / `516e5f374ff1` | 2099.2 | 366.2 | 全部车窗关闭，无动作/任务 |
| 否定更正 / `221ef641bcb2` | 1420.9 | 241.1 | 仅主驾massage=relax，无加热替代 |
| 闲聊 / `589152a1be61` | 1327.7 | 239.2 | 无动作/任务，首非零后有意停止 |
| 天气接手 / `c76553716b7a` | 1796.3 | 304.6 | 登记一个quick任务后确认接手 |
| 任务中切话题 / `5e6bdb36b8e8` | 1528.1 | 482.4 | 回答四门车窗全关，无旧任务误绑 |

七条ASR最终文本与fixture内容匹配；当前query的独立语义/命令/状态/PCM身份及回执顺序为7/7。主延迟预算≤1500ms仅2/7；≤2000ms为5/7，仅作次级诊断，不能代替当前目标。闲聊服务端取消trace322已收到，未观察到迟到PCM；未观察到不等于证明所有网络时序均无迟到。所有时钟仍为程序WS接收与模拟drain，不计为浏览器或扬声器测量。

**整体未通过（脚本exit1）**：天气任务 `cb6a62e71f75` 运行四步后failed，trace472记录“任务达到推理步骤上限，已停止；没有宣称完成。”虽然真实读取了法国气象局巴黎/12区页面，未产生finish_task完成报告或spoken_summary，固定后台完成预期未满足，不能把接手成功计为任务完成。主动轮 `cd3e9514e52b`、playback `e29cd8a0f7a2437c8c43ef12ead28181` 实际播报失败（trace478），267744字节真实PCM按程序队列规则结束；没有虚构天气结论。

首次失败已报告，所有原始记录保留；没有修改gold、重试替换或覆盖Pro记录。后续修复必须单独标记版本与新证据，不能把此批七条query通过描述为整批交付成功。


## Mini候选语音批次（2026-10-13，文件名final，实际失败）

`../docs/voice-interactions-mini-final-20261013.json` 为候选验证证据，**文件名final不表示验收通过**。会话 `e78b5437bc244524`，base8003；同七条query与缓存fixture，在catalog同步执行69个多轮检查的并行负载下运行，不是空载延迟。程序首非零PCM依次2440.8、3006.3、2254.6、1762.3、2099.1、2311.6、5564.8ms；≤1500ms为0/7，原始值不改，仍不是浏览器/声学测量。

原oracle自动query通过4/7；离线v3修正只读查询误判后为5/7，仍整体失败。原JSON完整保留，修正单独写入 `../docs/voice-interactions-mini-final-20261013-oracle-v3.json` 并记录原SHA-256。只允许windows问题的真实 `get_window_state` 回执作为读操作：每项command_id、turn、函数名、参数、ok、返回开度及完整四窗状态必须与快照一致；重复读、错值、缺回执、错turn或额外写操作不豁免。26项离线oracle测试通过，无新增provider请求。

实际问题：复合轮 `7d54e89d9c80` 在开空调/关窗外，调用四座位heating=0并重复主驾加热关闭，未启用按摩（模型trace1980）；否定更正 `60c6bec3e74f` 同样只关闭主驾加热（trace2028）。原转写仅有标点差异，词序与需求保留。此为真实错误工具选择，不能因状态本来为0而认为没有额外操作。车窗切话题 `062a9b6fda3f` 的四个只读查询回执均准确，最终回答全窗关闭；这项是oracle读写分类假失败，并非旧任务误绑。

天气任务 `169ffa9fa852` completed三步、read_details=false，但没有调用get_weather_forecast，也没有读取MétéoFrance官方域名。两次lookup_web实际读取climate-data.org、aqi.in、MeteoArt和LaChaîneMétéo月度页；finish仅引用MeteoArt。人工查看raw，MeteoArt的10月14日日历项为“Pluie légère 22°/17°”，与后面的历史月均17°/10°不同，因此不能把17–22直接断言为捏造或历史平均；但它不满足所要求的官方来源，亦没有本oracle可验证的ISO/时区/单位结构化forecast。口语实际已说“未找到法国气象局官网的直接预报”，原v2限制词法未识别此表达，相关“限制缺失”标签需人工注明；这不消除未读取官方页的真实失败。

本批未重试、未覆盖历史数据，后续修复必须另存证据。当前无充分依据将候选作为语音全验收成功交付。


## Mini固定语音末批（2026-10-13 run3；2026-10-14人工审计）

原始证据为 `../docs/voice-interactions-mini-verified-run3.json`，独立人工审计为 `../docs/voice-interactions-mini-verified-run3-human-audit.json`。原JSON未重写，SHA-256为 `1ef0096d8b014e80479dfc9649c5077b7602269d75c6b4e569d30a77f0115ded`；会话 `d3bddc57e7f94637`。同原七段缓存fixture、v3 oracle，与catalog固定v6的19轮文本检查并行，不是空载样本。实际Talker及本任务quick Thinker均为 `doubao-seed-2-0-mini-260428`。

| 固定场景 | 提交→程序首非零PCM ms | ASR stop→程序final ms | 人工语义核对 |
| --- | ---: | ---: | --- |
| 单车控 | 2199.4 | 341.3 | 仅all温控23，允许已声明的空调自动开启 |
| 复合车控 | 3142.2 | 357.1 | 开空调、关全部窗、主驾按摩relax，全部真实成功回执 |
| 车窗状态问答 | 1441.4 | 312.6 | 全部窗关闭，无车控或任务调用 |
| 否定更正 | 1751.1 | 295.9 | 仅主驾按摩relax，无加热替代或关闭动作 |
| 闲聊 | 2224.5 | 325.0 | 无车控/任务；首非零PCM后有意取消 |
| 天气接手 | 2131.3 | 395.0 | 一次实际登记quick任务，read_details=false，登记后才接手音频 |
| 任务中切话题 | 2751.1 | 406.9 | 回答所有窗关闭，无无关读写或旧任务操作 |

前台独立语义、状态差、回执、身份及传输检查 **7/7通过**，ASR内容 **7/7匹配**，传输错误及已观测迟到丢弃均为空。≤1500ms仅 **1/7**，范围1441.4–3142.2ms、P50 2199.4ms；性能目标未达到。ASR的295.9–406.9ms尾部等待另计。这些是Python WS接收时钟和程序模拟drain；本批未使用实体麦克风、浏览器Worklet或声学测量，不能替代真实用户录音/扬声器体验。

天气任务 `46d8f9031d32` 四步completed。trace3236返回Open-Meteo真实forecast，目标日 `2026-10-14`、法国巴黎、`Europe/Paris`、`°C`、最低11.2/最高19.3，与原始daily.time、daily_units和数值全部一致；目标日按任务2026-10-13登记时间确定。官方Search trace3245实际使用 `include_domains:[meteofrance.com]`、`include_domains_mode:restrict`，Extract trace3247真实读取两条官网URL。原材料是2026年4月热浪回顾及9月气候推荐内容，不能核实巴黎目标日温度。

完整报告明确温度“来自Open-Meteo”，并说“因此无法从法国气象局官网核实明天巴黎的温度”。口语明确“根据Open-Meteo的预报最低11.2度，最高19.3度”，随后说“没找到对应明天的具体温度数据，没法从该官网核实”。trace3278的实际 `audio.text` 与这份spoken_summary完全一致，来源为 `thinker_spoken_summary`。官方目标日预报没有取得，结果属于有真实API温度并如实披露官网限制；没有把API叫作法国气象局预报。

原v3自动报告保留两条“官方目标日限制缺失”失败，脚本整批仍返回失败。人工审计确认两项为词形漏判：规则只识别紧邻“无法核实”，遗漏“无法从法国气象局官网核实”，也不识别“没找到/没法从该官网核实”。独立审计保留原计数、完整原文、trace及JSON pointer，没有修改oracle或原报告来追求通过。该人工澄清不消除官网目标日预报未取得或1500ms性能失败，不能宣称全面验收通过。此后没有重跑live或改生产代码。
