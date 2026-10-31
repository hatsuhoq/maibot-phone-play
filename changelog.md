# 更新日志

## 0.1.2

- **iPhone 可以不用数据线了**：用 pymobiledevice3 通过 Wi-Fi 启动 WebDriverAgent，不需要管理员权限，拔线后速度和插线时一样。步骤见 README「准备工作（iPhone）」里的「不用数据线」。
- 联机游戏：在好友房、组队房间里等人、等开局时不再结束；`together` 也包括各用各的手机联机组队，不只是同一台手机轮流玩。
- 应用新增 `keep_open`：玩完不回桌面、打开时不先关掉，适合联机游戏；其他应用在上次结束后 15 分钟内再打开，也接着上次的画面。
- `wait` 最长 120 秒；超过 30 秒的等待只截图比对、不调用模型，有人进房间、开局了或聊天里有新要求时提前醒来。
- 聊天里的她知道手机上的实际情况：玩手机期间，发起的聊天每一轮都带上手机上的近况（`maisaka.planner.before_request`、`maisaka.replyer.before_request` Hook），不再编造进展。
- 玩的时候再调用 `play_phone`，会把对方的新要求交给手机上的她，下一步优先照做。
- 工具用法写进 `description`（宿主已不再使用 `detailed_description`）；`play_phone` 写明也能玩手机上的游戏。
- 点之前核对画面：要点的东西已经不在原处时，这一步不点，避免误点。
- iPhone 横屏应用里的返回手势按当前屏幕尺寸计算。
- 按上次审核建议：`usage.json` 损坏时自动备份、从空记录重新开始，不再导致插件加载失败；`device.adb_path` 的说明写明只填 adb 程序本身。

## 0.1.1

- 按插件中心审核意见调整：manifest 加上 `plugin_type: game`；`device.serial`、`device.wda_url` 的说明提醒别指向不可信的主机；README 开头提醒互动功能的风险。

## 0.1.0

- 首个版本：通过 adb 控制安卓手机或模拟器，或通过 WebDriverAgent 控制 iPhone（不需要 Mac）；Bot 按人设和兴趣自己决定玩什么，用视觉模型一步步操作。
- 聊天工具 `play_phone`、`phone_activity`、`stop_phone`、`show_phone_screen`；命令 `/手机`、`/手机截图`、`/手机 开始|暂停|继续|停止|帮助`。
- 和她一起玩（`together`）：同一台手机上轮流操作，等对方时不调用模型。
- 互动（`interaction`，默认关闭）：用她自己的账号点赞、评论、关注，按每日额度限制并检查评论内容。
- WebUI「手机实时画面」；空闲时自动玩（默认关闭）。
- 安全：应用白名单与前台检查、文字输入限制、单次与每日额度、连续出错自动结束；手机短暂断开时等待恢复。
