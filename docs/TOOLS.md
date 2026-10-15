# 模拟车控工具目录

统一目录在 `backend/catalog.py`，精确包含100个唯一函数、10个skill。热工具是其中15个函数的子集，其余85个只能通过Pro车控规划层使用。Talker调用`delegate_vehicle_control`，系统传递未经改写的整句；`load_skill`仅在Pro内按子系统读取详细目录，不计入100个车端函数，也不扩展Talker工具集合。

所有车控均只修改会话内模拟状态，不接触真实车辆。车机收到command后回传执行确认，由服务端模拟车端校验并执行，返回结构化result和完整新状态。浏览器展示按skill分组的状态，可直接核对结果。导航只记录目的地、途经点与偏好，不进行真实路线计算、订座或支付。

## 执行约束

- 本模拟为左舵车：`left_front`/左前/主驾对应`driver`，`right_front`/右前/副驾对应`front_passenger`；左后为`rear_left`，右后为`rear_right`。参数schema使用这些标准枚举值。
- `execute_tool(name, arguments, state)`使用JSON Schema验证；参数不能缺失、类型错误、额外增加或越界。所有数字必须有限。
- 先在状态副本中执行，全部成功才提交。校验失败、安全拒绝、资源不足、相对调整越界都保留原状态；批量操作也原子执行。
- 回执包含`ok/name/arguments/value或reason/state`。读取函数不修改状态，成功回执保留执行后的真实模拟值。
- `_meta.speed_kph`默认0。行驶中禁止开尾门、解锁门、调整主驾座椅/方向盘和恢复座椅记忆；超过5km/h不能折叠镜，超过80km/h车窗/天窗开度不能超过50%。
- 后排车窗童锁阻止打开后排窗；关闭全部车窗仍允许。折叠后视镜不能调角度。充电中不能解锁充电口。玻璃水耗尽不能清洗。
- 温度单位显示偏好不会改变控制输入的摄氏度单位。温度、风量、音量和开度相对调整超界会拒绝，不静默截断。

## 热工具

这些schema常驻Talker的同一次模型调用。其余函数仅由Pro按子系统读取详细schema。

`set_climate_power`, `set_temperature`, `set_fan_speed`, `set_window_position`, `close_all_windows`, `set_seat_heating`, `set_interior_light`, `set_volume`, `control_playback`, `set_navigation_destination`, `adjust_temperature`, `set_seat_ventilation`, `set_seat_massage`, `set_seat_position`, `set_ambient_light`

冷热混合请求整体委托Pro，不先执行热部分。Pro可调用入口仅`load_skill`与`submit_vehicle_plan`，车辆函数及实际参数schema嵌入计划actions。计划由actor整组预检、按依赖串行下发，再根据实际回执生成每项完成/失败/保持账本。结构与原文来源校验不能代替独立语义验收。

## 完整函数清单

全部schema、状态映射、示例也可通过`GET /api/config`或`load_skill`读取。下表参数列给出必填参数及其范围；完整JSON Schema以目录为准。

### 空调与空气管理 `climate` — 18个

温区温度、风量、送风、内外循环、除雾、空气净化与香氛。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_climate_state` | 读取空调与空气管理全部状态 | 无 | `{}` |
| `set_climate_power` ★ | 开启或关闭空调电源 | enabled: boolean | `{"enabled":true}` |
| `set_temperature` ★ | 设置温区目标温度，单位摄氏度，自动打开空调 | zone: all/driver/front_passenger/rear; temperature: 16..30 | `{"zone":"all","temperature":24}` |
| `adjust_temperature` ★ | 相对调高或调低温区温度，超范围拒绝整条指令 | zone: all/driver/front_passenger/rear; delta: -5..5 | `{"zone":"driver","delta":-1}` |
| `set_fan_speed` ★ | 设置空调风量，0关闭风机，7最大 | level: 0..7 | `{"level":4}` |
| `adjust_fan_speed` | 相对调整空调风量 | delta: -3..3 | `{"delta":1}` |
| `set_airflow_mode` | 设置吹面、吹脚、挡风除雾或混合送风 | mode: face/feet/defrost/mixed | `{"mode":"feet"}` |
| `set_recirculation` | 设置内循环、外循环或自动循环 | mode: internal/external/auto | `{"mode":"internal"}` |
| `set_auto_climate` | 开关自动空调模式 | enabled: boolean | `{"enabled":true}` |
| `set_front_defrost` | 开关前挡风玻璃除雾 | enabled: boolean | `{"enabled":true}` |
| `set_rear_defrost` | 开关后挡风玻璃加热除雾 | enabled: boolean | `{"enabled":true}` |
| `set_air_purifier` | 开关车内空气净化器 | enabled: boolean | `{"enabled":true}` |
| `set_fragrance` | 设置香氛浓度，0关闭，3最高 | level: 0..3 | `{"level":1}` |
| `set_humidity` | 设置模拟车内目标相对湿度 | target: 30..70 | `{"target":45}` |
| `set_temperature_unit` | 设置温度显示单位，控制温度仍使用摄氏度 | unit: celsius/fahrenheit | `{"unit":"fahrenheit"}` |
| `sync_climate_zones` | 开关温区同步，打开时所有温区跟随主驾温度 | enabled: boolean | `{"enabled":true}` |
| `set_rear_climate_power` | 开关后排独立空调 | enabled: boolean | `{"enabled":true}` |
| `get_air_quality` | 读取模拟PM2.5与CO2空气质量数值 | 无 | `{}` |

### 座椅与舒适 `seats` — 16个

四个座位的加热、通风、按摩、位置、记忆与方向盘舒适设置。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_seat_state` | 读取指定座位状态 | seat: driver/front_passenger/rear_left/rear_right | `{"seat":"driver"}` |
| `set_seat_heating` ★ | 设置指定座椅加热，0关闭，3最高 | seat: driver/front_passenger/rear_left/rear_right; level: 0..3 | `{"seat":"driver","level":2}` |
| `set_seat_ventilation` ★ | 设置指定座椅通风，0关闭，3最高 | seat: driver/front_passenger/rear_left/rear_right; level: 0..3 | `{"seat":"driver","level":2}` |
| `set_seat_massage` ★ | 设置座椅按摩模式 | seat: driver/front_passenger/rear_left/rear_right; mode: off/relax/lumbar/wave | `{"seat":"driver","mode":"relax"}` |
| `set_seat_position` ★ | 设置座椅轴向绝对位置，主驾停车才可调；靠背80至140度，其余0至100 | seat: driver/front_passenger/rear_left/rear_right; axis: fore_aft/height/backrest/lumbar; position: 0..140 | `{"seat":"driver","axis":"fore_aft","position":60}` |
| `adjust_seat_position` | 相对调整座椅位置，主驾停车才可调 | seat: driver/front_passenger/rear_left/rear_right; axis: fore_aft/height/backrest/lumbar; delta: -10..10 | `{"seat":"driver","axis":"height","delta":5}` |
| `save_seat_memory` | 保存座椅设置到指定记忆槽位 | seat: driver/front_passenger/rear_left/rear_right; slot: 1..3 | `{"seat":"driver","slot":1}` |
| `recall_seat_memory` | 停车后恢复指定座椅记忆槽位 | seat: driver/front_passenger/rear_left/rear_right; slot: 1..3 | `{"seat":"driver","slot":1}` |
| `set_steering_heating` | 开关方向盘加热 | enabled: boolean | `{"enabled":true}` |
| `set_steering_position` | 停车后调节方向盘伸缩或高度 | axis: reach/height; position: 0..100 | `{"axis":"reach","position":60}` |
| `set_easy_entry` | 开关座椅迎宾模式 | enabled: boolean | `{"enabled":true}` |
| `set_seat_occupancy` | 模拟指定座椅乘员占用状态，供车控状态验收 | seat: driver/front_passenger/rear_left/rear_right; occupied: boolean | `{"seat":"rear_left","occupied":true}` |
| `get_seat_memory` | 读取指定座椅记忆槽位 | seat: driver/front_passenger/rear_left/rear_right; slot: 1..3 | `{"seat":"driver","slot":1}` |
| `set_seat_cushion_length` | 设置座垫长度 | seat: driver/front_passenger/rear_left/rear_right; position: 0..100 | `{"seat":"driver","position":2}` |
| `set_seat_side_support` | 设置座椅侧翼支撑档位 | seat: driver/front_passenger/rear_left/rear_right; level: 0..5 | `{"seat":"driver","level":2}` |
| `set_seat_headrest` | 设置头枕高度 | seat: driver/front_passenger/rear_left/rear_right; position: 0..100 | `{"seat":"driver","position":2}` |

### 车窗 `windows` — 10个

四门车窗开合百分比、童锁、防夹、天窗、遮阳帘与雨天自动关窗。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_window_state` | 读取指定车窗开度，0全关，100全开 | window: driver/front_passenger/rear_left/rear_right | `{"window":"driver"}` |
| `set_window_position` ★ | 设置单扇车窗开度百分比，0全关，100全开；童锁/高速状态可能拒绝 | window: driver/front_passenger/rear_left/rear_right; position: 0..100 | `{"window":"driver","position":50}` |
| `adjust_window_position` | 相对调整单扇车窗开度百分比 | window: driver/front_passenger/rear_left/rear_right; delta: -100..100 | `{"window":"driver","delta":20}` |
| `close_all_windows` ★ | 关闭全部四门车窗 | 无 | `{}` |
| `open_all_windows` | 打开全部四门车窗，任一车窗不安全则整条拒绝 | 无 | `{}` |
| `set_window_child_lock` | 开关后排车窗童锁，开启后不能打开后排车窗 | enabled: boolean | `{"enabled":true}` |
| `set_window_anti_pinch` | 停车后开关指定车窗防夹模拟设置 | window: driver/front_passenger/rear_left/rear_right; enabled: boolean | `{"window":"driver","enabled":true}` |
| `set_sunroof_position` | 设置天窗开度，0关闭，100全开 | position: 0..100 | `{"position":30}` |
| `set_sunshade_position` | 设置天窗遮阳帘开度，0遮住，100打开 | position: 0..100 | `{"position":50}` |
| `set_rain_close_windows` | 开关雨天自动关闭车窗的模拟偏好 | enabled: boolean | `{"enabled":true}` |

### 后视镜 `mirrors` — 8个

左右后视镜角度、折叠、加热、倒车下翻与记忆。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_mirror_state` | 读取指定后视镜位置及公共设置 | mirror: left/right | `{"mirror":"left"}` |
| `set_mirror_fold` | 折叠或展开左右后视镜，车速超过5时拒绝折叠 | folded: boolean | `{"folded":true}` |
| `set_mirror_angle` | 设置指定后视镜水平和垂直角度，范围-20至20度 | mirror: left/right; horizontal: -20..20; vertical: -20..20 | `{"mirror":"left","horizontal":2,"vertical":-3}` |
| `adjust_mirror_angle` | 相对调整指定后视镜单轴角度 | mirror: left/right; axis: horizontal/vertical; delta: -10..10 | `{"mirror":"left","axis":"vertical","delta":-2}` |
| `set_mirror_heating` | 开关两侧后视镜加热除雾 | enabled: boolean | `{"enabled":true}` |
| `set_reverse_mirror_tilt` | 开关倒车时后视镜自动下翻偏好 | enabled: boolean | `{"enabled":true}` |
| `save_mirror_memory` | 保存左右后视镜角度记忆 | slot: 1..3 | `{"slot":1}` |
| `recall_mirror_memory` | 恢复左右后视镜角度记忆 | slot: 1..3 | `{"slot":1}` |

### 灯光 `lights` — 12个

车内灯、阅读灯、氛围灯及颜色亮度、大灯、雾灯、双闪与伴我回家。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_light_state` | 读取车内外灯光全部状态 | 无 | `{}` |
| `set_interior_light` ★ | 开关全部、前排或后排车内照明灯 | zone: all/front/rear; enabled: boolean | `{"zone":"all","enabled":true}` |
| `set_ambient_light` ★ | 开关氛围灯 | enabled: boolean | `{"enabled":true}` |
| `set_ambient_color` | 设置氛围灯颜色 | color: white/warm/red/blue/green/purple | `{"color":"blue"}` |
| `set_ambient_brightness` | 设置氛围灯亮度 | brightness: 0..100 | `{"brightness":60}` |
| `set_headlight_mode` | 设置大灯关闭、自动或近光模式 | mode: off/auto/low | `{"mode":"auto"}` |
| `set_high_beam` | 开关远光灯 | enabled: boolean | `{"enabled":true}` |
| `set_front_fog_lights` | 开关前雾灯 | enabled: boolean | `{"enabled":true}` |
| `set_rear_fog_light` | 开关后雾灯 | enabled: boolean | `{"enabled":true}` |
| `set_reading_light` | 开关指定座位阅读灯 | seat: driver/front_passenger/rear_left/rear_right; enabled: boolean | `{"seat":"rear_left","enabled":true}` |
| `set_follow_me_home` | 设置伴我回家大灯延时，0关闭 | seconds: 0..120 | `{"seconds":60}` |
| `set_hazard_lights` | 开关双闪危险警告灯 | enabled: boolean | `{"enabled":true}` |

### 门锁 `locks` — 8个

四门门锁、儿童安全锁、自动落锁、离车上锁与充电口锁。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_lock_state` | 读取全部门锁和安全锁状态 | 无 | `{}` |
| `set_door_lock` | 锁定或解锁指定车门或全部车门，行驶中拒绝解锁 | door: all/driver/front_passenger/rear_left/rear_right; locked: boolean | `{"door":"driver","locked":false}` |
| `lock_all_doors` | 锁定全部四门 | 无 | `{}` |
| `unlock_all_doors` | 停车后解锁全部四门 | 无 | `{}` |
| `set_child_lock` | 停车后开关后排左右车门儿童安全锁 | side: left/right; enabled: boolean | `{"side":"left","enabled":true}` |
| `set_auto_lock` | 开关行车自动落锁偏好 | enabled: boolean | `{"enabled":true}` |
| `set_walk_away_lock` | 开关离车自动上锁偏好 | enabled: boolean | `{"enabled":true}` |
| `set_charge_port_lock` | 锁定或解锁充电口，充电中拒绝解锁 | locked: boolean | `{"locked":false}` |

### 尾门 `tailgate` — 4个

电动尾门开关、开启高度与停止，行驶中不能打开。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_tailgate_state` | 读取尾门开合与高度状态 | 无 | `{}` |
| `set_tailgate_open` | 开启或关闭尾门，模拟执行立即完成，行驶中拒绝开启 | open: boolean | `{"open":true}` |
| `set_tailgate_height` | 设置尾门下次开启高度百分比 | height: 20..100 | `{"height":80}` |
| `stop_tailgate` | 停止模拟尾门运动并保留当前位置 | 无 | `{}` |

### 雨刷清洗 `wipers` — 6个

前后雨刷模式、间隔、雨量灵敏度与玻璃清洗液模拟消耗。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_wiper_state` | 读取雨刷与玻璃水状态 | 无 | `{}` |
| `set_wiper_mode` | 设置前雨刷关闭、自动、间歇、低速或高速 | mode: off/auto/intermittent/low/high | `{"mode":"auto"}` |
| `set_wiper_interval` | 设置间歇雨刷间隔秒数 | seconds: 1..15 | `{"seconds":3}` |
| `wash_windshield` | 清洗前或后挡风玻璃，每次模拟消耗1%玻璃水，耗尽则拒绝 | target: front/rear | `{"target":"front"}` |
| `set_rear_wiper` | 开关后雨刷 | enabled: boolean | `{"enabled":true}` |
| `set_rain_sensitivity` | 设置自动雨刷雨量灵敏度 | level: 1..5 | `{"level":4}` |

### 媒体 `media` — 10个

模拟音量、静音、播放状态、曲目、音源、FM频率、声场与均衡器。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_media_state` | 读取模拟媒体状态 | 无 | `{}` |
| `set_volume` ★ | 设置媒体音量百分比 | volume: 0..100 | `{"volume":40}` |
| `adjust_volume` | 相对调高或调低媒体音量 | delta: -30..30 | `{"delta":5}` |
| `set_mute` | 开关媒体静音 | enabled: boolean | `{"enabled":true}` |
| `control_playback` ★ | 播放、暂停、停止、下一首或上一首，仅更新模拟媒体状态 | action: play/pause/stop/next/previous | `{"action":"pause"}` |
| `set_media_source` | 切换蓝牙、广播、USB或在线模拟音源 | source: bluetooth/radio/usb/online | `{"source":"radio"}` |
| `play_media` | 按名称播放模拟曲目；不进行真实版权内容搜索或音频播放 | title: string | `{"title":"夜曲"}` |
| `set_radio_frequency` | 设置模拟FM广播频率，单位MHz | frequency: 87.5..108 | `{"frequency":101.7}` |
| `set_audio_balance` | 设置音响左右平衡和前后声场，负数偏左或前，正数偏右或后 | balance: -10..10; fader: -10..10 | `{"balance":0,"fader":-2}` |
| `set_equalizer` | 设置模拟音效均衡器预设 | preset: flat/pop/rock/classical/voice | `{"preset":"voice"}` |

### 导航 `navigation` — 8个

只保存模拟目的地、最多10个途经点、通行偏好及家和公司地址；不计算真实路线。

| 函数 | 中文说明 | 必填参数 | 示例 |
|---|---|---|---|
| `get_navigation_state` | 读取模拟导航目的地、途经点与偏好，不含真实路线 | 无 | `{}` |
| `set_navigation_destination` ★ | 设置并开始模拟导航目的地；不计算真实路线、不订座支付 | destination: string | `{"destination":"巴黎卢浮宫"}` |
| `add_navigation_waypoint` | 追加一个模拟途经点，最多10个，不计算真实路线 | destination: string | `{"destination":"附近充电站"}` |
| `remove_navigation_waypoint` | 删除指定索引的模拟途经点，索引从0开始 | index: 0..9 | `{"index":0}` |
| `set_navigation_preferences` | 保存模拟避收费、避高速与优先最快偏好；不计算真实路线 | avoid_tolls: boolean; avoid_highways: boolean; prefer_fastest: boolean | `{"avoid_tolls":true,"avoid_highways":false,"prefer_fastest":true}` |
| `cancel_navigation` | 结束模拟导航并清空当前目的地和途经点 | 无 | `{}` |
| `set_home_destination` | 保存家的模拟导航地址 | destination: string | `{"destination":"巴黎市中心"}` |
| `set_work_destination` | 保存公司的模拟导航地址 | destination: string | `{"destination":"拉德芳斯"}` |

## 扩展到500–600个函数

新增skill manifest及函数注册即可扩展同一目录。每个函数必须有唯一名称、中文说明、严格JSON Schema、skill归属、state_mapping、调用示例和执行器；执行器只修改候选副本。热工具保持少量高频子集，其余仅由Pro通过load_skill按子系统加载，实际说明与参数schema进入计划actions。当前原型按最新范围不实现embedding检索。

新增函数后应更新预期分组数量并执行目录测试；用参数边界、状态拒绝和设置后读取验证实际模拟行为。
