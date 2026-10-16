# 媒体、导航和端侧查询

新版本独立启动：

```sh
VITE_PORT=5174 VITE_API_TARGET=http://127.0.0.1:8001 npm run dev
```

`VITE_PORT` 与 `VITE_API_TARGET` 也可放在 `frontend/.env.local`。省略时继续使用 5173 / 8000。浏览器每个端口有独立会话存储。

播放器使用真实 HTML audio/video。播放回执等待 `play()` promise 和 `playing`，跳转等待 `seeked`，暂停读取真实元素状态。浏览器阻止自动播放、网络加载/解码失败、直播不可 seek 时明确返回失败。网络电台必须有真实流地址；模拟 FM 频率不构成可播放来源。目录/API provider 未配置时显示未配置，不自动替换成开发媒体。音乐、视频、网络电台分别读取自己的 CP binding 与连接状态；公开内容 ID 带类型和 binding，不能用另一类 CP 的同名 ID 替代。

音频焦点：TTS 降低音乐/电台音量，视频暂时暂停；VAD 发现用户说话时暂停媒体；监听空闲时降低音量。释放焦点只恢复仍保持播放意图的同一媒体，用户主动暂停、退出、换源或连接中断之后不能迟到恢复。

查询和控制使用独立语义。`cockpit.command.kind=query` 只返回 `receipt.result`，不进入播放状态/版本合并；`kind=control` 由实际媒体或模拟导航执行后回传 `receipt.state`。所有回执保留 request_id、command_id、session_id、turn_id、target_device、kind。端侧查询具有 deadline 与 timeout，迟到结果不回填。

当服务明确选择 `device_catalog` 时，可在面板导入真实本机音视频文件；文件通过浏览器 object URL 播放，不上传云端。端侧媒体查询返回内容 ID 与元数据，URL 能力只留在浏览器。端侧本机目录也可通过 `window.superXDCockpitDevice.setMediaCatalog(items)` 显式加载真实目录条目。

本机 POI/导航 SDK 需要显式注册查询 handler；默认没有本机 GPS 或 POI 搜索能力，也不会回退云 CP 冒充端侧结果：

```js
const unregister = window.superXDCockpitDevice.register('places', 'search_places',
  async (arguments_, { signal, command }) => {
    // 调用已配置的本机 SDK/API，并原样保留供应商与坐标系身份。
    return await configuredDeviceSdk.searchPlaces(arguments_, { signal });
  });
```

POI 返回值应包含真实 `provider`、`provider_id`、`place_ref`、`coordinate_system` 等身份。handler 是设备集成入口，不是内置模拟搜索。查询失败/不支持时返回明确错误。候选卡可设为出发地、启动模拟导航或添加途经点；面板支持移除途经点、取消导航和查询路线。控制导航始终是模拟座舱起点/目的地/途经点；路线与 ETA 仅展示实际配置路线 provider 返回的数据。没有已确认起点或路线服务失败时，保留明确的不可用状态。
