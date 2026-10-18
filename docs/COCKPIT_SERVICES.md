# 座舱媒体、导航与设备网关

云端 agent 负责模型、结构化路由与会话状态；内容查询和设备执行是独立接口。工具按两维声明：`operation=query|control` 与 `execution_location=cloud|device`。在设备上运行的搜索仍是只读查询，不能当成播放器或导航已执行。每轮动态快照里的 `tool_capabilities` 给出这些元数据，工具名字固定为新增的 `search_media`、`control_media`、`manage_navigation`，不随提供商或对话增加。

## 媒体来源

默认腾讯音乐方向通过**操作者提供的企业网关**对接。项目没有假定腾讯存在某个公开 API，也没有内置腾讯曲库、账户或授权。配置必须提供网关 URL 与所选授权策略要求的环境变量密钥；公共无鉴权网关必须显式设置 require_api_key=false，timeout_seconds 实际作用到HTTP请求。未配置时明确 `not_configured`，不会回退模拟音乐。

网关协议由本项目定义，需企业网关实现：

- `POST /v1/media/search`，Bearer 授权，请求 `{kind,query,max_results}`；响应 `{ok:true,items:[{content_id,kind,title,artist?,album?}]}`。
- `POST /v1/media/resolve`，请求 `{content_id}`；响应 `{ok:true,item:{content_id,kind,title,source_url,...}}`。返回 ID 必须与请求一致；临时播放 URL 每次解析，不作为模型参数。真实请求/响应以脱敏 _provider_trace 进入诊断存储，签名地址作为播放能力隐藏；该私有诊断不进入模型上下文。

`kind` 是 music、video 或 radio；radio 是有真实可播地址的网络电台，不冒充 FM 硬件调谐。

显式 `local_catalog` 开发配置仅使用用户配置的 HTTP(S) URL 或受保护的 `/api/media/files/...` 文件。目录条目不由模型编造。`device_catalog` 是端侧查询标记：云端 provider 不持有设备会话，Session 发设备查询，本机目录实际返回内容，再由本机播放器通过 content_id 解析资源。设备未装载目录或查询 handler 时失败，不静默改用云目录。

## 查询与控制 wire

`cockpit.command` 同时承载 query 和 control，必填：

```json
{"command_id":"opaque-id","request_id":"opaque-id","session_id":"session","turn_id":"turn","target_device":"browser:session","kind":"query","execution_location":"device","domain":"media","action":"search","arguments":{"kind":"music","query":"器乐"},"timeout_ms":12000,"deadline_at":"UTC ISO8601"}
```

设备必须 echo `command_id/request_id/session_id/turn_id/target_device/kind`。查询成功用 `cockpit.receipt.result`，不合并到播放状态、不增加播放版本。支持 media/search、media/resolve、media/get_status 与 places/search_places；设备查询 registry 需显式注册实际 handler。

控制额外携带 `expected_state_version`。media 动作包括 play/pause/resume/stop/exit/next/previous/seek/volume；play/next/previous 的内容只能来自实际目录。实际 HTML 媒体事件和 `play()` Promise 决定回执，浏览器自动播放失败、资源失败、seek 不支持均不得报成功。成功回执携带实际 `state` 及 `state_version=expected+1`。暂停、停止、退出含义分别为保留进度、停止保留内容、释放播放器。

Session 只在 handle 内更新状态；异步查询结果 post 回队列。同轮同域控制串行等待回执。请求错会话、错目标、错轮次、重复或超时回执丢弃。设备版本冲突会触发一次独立 get_status 查询，再重准备原来未执行的操作，最多一次；不重放已成功动作。

前端 UI 可发 `cockpit.control {domain,action,arguments}`。媒体 action=search 走同一查询路由；用户选择内容发 play+content_id。`cockpit.media_state` 是同版本的实际进度/音频焦点观察，不能覆盖新版本或改变用户 desired_status；真实 HTML ended 事件显式携带 event=ended，并把同版本同内容状态与意图同步为 stopped。TTS/VAD 对媒体的 duck/pause 恢复由前端音频焦点层执行，手动暂停不会被自动恢复。

## 导航

地点同时保留原始 `place_id`、`provider_id`、`provider`、`place_ref=provider:provider_id`、`coordinate_system` 和来源。Session 内用 scoped 引用；旧裸 ID 只有本会话无歧义时兼容。语音不能选同轮刚搜到但尚未展示/确认的候选；UI 点击本身是明确选择。

manage_navigation 支持 set_origin/start/add_waypoint/remove_waypoint/cancel/get_status/route。途经点上限十个，按真实 place_ref 删除。导航面板执行仍明确 `mode=simulated`；设备展示并返回匹配的起点/目的地/途经点回执，才更新该状态。

route 独立调用配置的真实路线 provider。起点优先本次明确指定的 origin_ref，其次已设置的 origin，最后真实设备坐标；缺起点、缺服务、坐标系不支持均明确失败，不猜 GPS，不编路线/距离/ETA。路线结果保留 provider/queried_at/coordinate_system，绑定查询时的导航版本；查询成功不等于导航设备已启动。提供商失败的 code/details 写入脱敏 trace，用户只听到路线或导航对应的简短失败提示。

## 验证边界

`tests/demo/test_cockpit.py` 验证内容来源、设备身份绑定、失败保留状态、过期隔离、控制串行、一次版本恢复和导航来源。`evals/cases/cockpit.json` 使用明确的测试目录及模拟设备回执，报告标记 `cockpit_device_mode=explicit_eval_fixture`；它验证真实模型的路由和参数，**不证明浏览器已经发出声音或腾讯授权已连通**。浏览器实际播放验收单独记录。

## 音乐、视频、电台 CP 隔离

`MediaServices(providers_by_kind, bindings)` 为三个 kind 分别绑定 provider；音乐默认腾讯企业网关，视频和电台分别使用自己的 CP 网关 profile，车书仍是独立的 ManualProvider 资源接口；embedding 仅是 local 车书实现的内部依赖。某个 CP 缺密钥不会改向另一个 CP 查询。开发模式可以显式把 LocalMediaCatalog 绑定到三个 kind。

公开内容 ID 为 `media:{kind}:{profile_id}:{urlsafe_base64(CP原始ID)}`，模型和播放控件只选择该 ID。resolve 解码后仅调用绑定 CP，验证返回的原始 ID 和 kind；不同 CP 或不同 kind 的相同原始 ID 不会串播。配置状态与执行位置按 `info.providers[kind]` 独立展示。

端侧搜索和解析返回本机原始 ID，由 broker 添加作用域；设备控制除 scoped content_id 外携带 `device_content_id` 供端侧 SDK 查真实资源，实际播放 state 仍回 scoped content_id。设备 URL/blob 留在设备上，不作为云端模型或播放器控制参数。


## 评测夹具迁移与结果解释

`multi_three_hot_actions` 原夹具只初始化 VehicleSimulator，检查模拟 `vehicle.media` 的暂停。生产现在注入独立播放器服务，模型选择真实 `control_media.pause` 是对应行为，因此该用例迁移到显式 `cockpit.media` 正在播放夹具；保留总共三个动作、一次模型决策、后排温度与主驾座椅两项真实模拟器回执，并增加播放器暂停的显式设备fixture回执和状态断言。没有删掉原来的车控或单次调用约束，也不把内容服务未配置时的失败当作成功。

完整首次报告 `pluggable-full.json` 保留迁移前的失败证据。`configured_location_nearby` 未改变来源原文约束，也未增加专用 prompt；模型把已配置英文地名翻译后被 grounding 拒绝的失败类型在旧基线已经出现，复测记录其采样波动。

旧热工具 `set_navigation_destination` 仍明确使用 VehicleSimulator 的地址导航模拟；新导航面板的 `manage_navigation` 使用已确认的 provider-scoped POI 引用。这两者的状态和执行来源在调试快照中分别展示，不伪造地址对应的坐标。
