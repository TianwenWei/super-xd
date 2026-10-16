# 可替换服务与座舱功能验证

2026-10-16，在独立 worktree/分支 `codex/pluggable-services-cockpit` 完成。原 main 服务8000/5173保留；新试用服务8001/5174使用独立数据库。本机试用显式设置 `MEDIA_PROFILE=local`，音乐、视频、电台样本均为本地生成验证素材，不表示外部 CP 已接通。

配置入口是 `config/services.toml`，接入与协议见 [服务配置](SERVICE_PROVIDERS.md)。音乐、视频、电台、Web Search、POI、车书是独立资源接口；查询/控制与云侧/端侧分别声明。设备执行仍以实际回执为准。

## 离线检查

- 全量后端测试：1386通过，日志 `data/pluggable-tests-release-final.log`。
- 前端：79通过，生产构建通过。
- 严格配置校验：18个主模板、104条原生示例消息、11个文字配置文件、697个文案条目。
- 运行配置校验通过；修改的50个Python文件没有正则导入，新增前端模块没有正则。
- 暂存差异检查没有环境密钥、`.env`、数据库、依赖或缓存文件。

协议测试使用明确的离线 HTTP/音频夹具，覆盖角色绑定、实际请求与原始响应、流式工具参数delta、截断/错误响应、超时与关闭权、独立CP及内容ID作用域、设备身份/版本/超时、坐标与天气回执、车书来源及向量索引一致性。

## 真实模型评测

全部用例重复三次：最终269/270（99.63%），报告 `data/evals/pluggable-final.json`，保留中间失败报告。新增座舱用例33/33；跨播放器与车控的三动作用例3/3，每轮一次模型调用。

原 `multi_three_hot_actions` 的模拟音乐夹具迁移为显式播放器夹具：仍要求同轮暂停媒体、调整温度和座椅三项真实回执，仍限定一次决策。没有删除动作或放松状态断言；旧模拟车控兼容测试保留。

除这3条迁移运行外，共同234条旧用例：基线232通过，最终233通过。首句指标是可送TTS时间，不是扬声器出声时间：

| 指标 | 最近基线 | 最终 |
|---|---:|---:|
| 首句P50/P90 | 10692 / 17657 ms | 2238 / 3827 ms |
| 总时长P50/P90 | 13252 / 29533 ms | 3630 / 9344 ms |
| 平均模型调用次数 | 1.23 | 1.26 |

供应商负载、缓存与采样会波动，以上不是固定加速承诺。没有加入额外串行模型阶段。新增33条媒体用例首句P50/P90为1992/3415ms，平均调用1.09；控制30条各一次，搜索3条各两次。

唯一剩余失败为 `speaker_cold_seat_support`：三次中一次误选车书检索代替冷车控规划，把真实说明书的手动使用方法当成模拟车能力，未下发车控。独立复查仍2/3，不能声称已消除；完整失败轨迹保留，prompt与few-shot没有针对该句打补丁。新增失败原因在此明确记录。

最终 speech review 189条，平均自然度4.25；154段动作前话术中提前宣布完成0次。此审查读文本，只作参考。真实 stop trace含26个工具、104条示例、原始tool_calls与实际设备version1/stopped回执，成功未重复播报；同次请求15810 prompt tokens中12856 cached tokens（81.3%）。

比较、话术与trace文件：`pluggable-final-comparison.json`、`pluggable-final.speech.json`、`pluggable-final-seat-recheck.json`、`cockpit-final-trace-audit.json`，均在本机 `data/evals/`，不提交私人对话与数据库。

## 实际服务与浏览器

- Chrome实际解码音乐WAV、640×360视频和12秒电台录制样本；暂停、恢复、seek、停止、退出及自然结束均核对浏览器状态和服务端回执。不同kind的scoped内容ID没有串播。
- 真实TTS期间视频暂停，结束后恢复；手动暂停与迟到恢复由专项测试覆盖。
- 独立端侧配置在8002/5175验收后已清理：通过原生文件选择导入真实WAV，云端Talker查询到端侧结果，再由设备实际播放blob音频。查询后仍closed/version0，实际播放回执才进入playing/version2；blob URL未进入云trace。已知搜索缓存命中，因此未宣称单独resolve wire也经过浏览器验收。未注册POI没有回退Google。
- 上述验收发现未注册能力被包装成普通故障；已修为保留 `unsupported_capability/retryable=false` 并记录全部查询失败的脱敏原始回执。200项相关回归通过，固定用户提示不会包含设备原始异常。
- 最终8001服务另做真实LLM请求与显式负向设备协议验证：trace保留实际109 messages、26 tools、模型/推理/预算请求体及真实SSE终止状态；设备即使错误标记retryable=true，unsupported能力仍不可重试，原始诊断仅进trace。证据 `data/final-live-request-audit.json`。
- 真实Google地点候选支持设置起点、开始模拟导航、增删途经点和取消。导航设备仍是模拟；没有推断GPS。
- Google Routes返回实际403 `PERMISSION_DENIED / SERVICE_DISABLED`，说明所用项目尚未启用该API。新导航失败话术正确，路线保持空，不编造距离/ETA。启用与Key权限由项目维护者操作，再用现有配置复测。
- 真实火山ASR/TTS：同一免按键连接连续两轮识别，均有非零24kHz PCM，第二轮VAD打断仍在生成的首轮音频。两次真实方舟流均正常收到终止标记，无model.error。
- 未测试物理麦克风、AEC、扬声器或量产车机SDK。FireRed/vLLM和外部音乐/视频/电台/车书网关目前只有适配协议与离线契约验收，尚无用户提供的真实自建端点或CP授权资料。
- 一次端侧搜索的模型文字回复附带内部内容ID；该轮TTS关闭，没有将这类回复误报为已通过口语验收。内容路由和播放器行为已验证，表达仍有改进空间。

浏览器与音频证据位于本机 `data/cockpit-browser-*.json`、`data/pluggable-audio-final.json`、同名trace及 `data/pluggable-cockpit.png`。车控、导航和FM硬件仍明确标记模拟，不代表接通真实车辆。
