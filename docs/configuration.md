# 配置说明

[← 返回 README](../README.md)

所有配置都可以在 WebUI「插件扩展 → 刷会儿手机」的配置页里改，保存后立即生效；也可以手动编辑插件目录下的 `config.toml`。下面按配置页的分节列出常用的项，每一项在配置页里都有说明。

## 插件

| 配置 | 默认值 | 说明 |
|---|---|---|
| `plugin.admin_user_ids` | 空 | 管理员账号 ID（例如 QQ 号），能用控制命令、截图、切换设备 |
| `plugin.screenshot_for_everyone` | 关 | 所有人都能用 `/手机截图`、让她截屏给自己看（截图里可能带有通知等隐私） |

## 设备

| 配置 | 默认值 | 说明 |
|---|---|---|
| `device.backend` | `adb` | `adb`=安卓，`ios_wda`=iPhone |
| `device.name` | 空 | 设备名字，切换时用；不写叫「默认」 |
| `device.adb_path` | `adb` | adb 的路径，不在 PATH 里时写完整路径 |
| `device.serial` | 空 | 安卓设备序列号或 `IP:端口`；无线调试的真机也可以只填 IP，端口变了插件会自己找；留空使用唯一已连接的设备 |
| `device.wda_url` | `http://127.0.0.1:8100` | iPhone 上 WebDriverAgent 的地址 |
| `device.text_input` | `adb_keyboard` | 安卓中文输入需要 ADBKeyboard，没装用 `ascii_only`；iPhone 不看这项 |
| `multi_device.devices` | 空 | 更多手机，写法和「设备」一样，每台都要写 `name`，见[多台设备](devices.md#多台设备) |

## 模型

| 配置 | 默认值 | 说明 |
|---|---|---|
| `model.vision_task` / `vision_model` | `vlm` / 空 | 看屏幕做决定的模型，必须能看图；`vision_model` 可以直接指定 model_config 里的某个模型（只影响本插件） |
| `model.planner_task` / `planner_model` | `utils` / 空 | 决定玩什么的文本模型 |

点得准不准基本取决于视觉模型。实测推荐 `gpt-6.1-terra`：点赞按钮、搜索框、评论输入框都点得准，会按人设和兴趣自己去搜、看完自己结束；iPhone 上 122 次调用平均每步 5 秒（中位 4.4 秒，最慢 18 秒），约 3700 tokens。

## 安全

| 配置 | 默认值 | 说明 |
|---|---|---|
| `safety.allowed_apps` | 哔哩哔哩、抖音、小红书、网易云音乐、番茄小说 | 白名单，只能打开这里的应用；每个应用还能单独设置，见下面 |
| `safety.extra_allowed_packages` | 空 | 额外允许停留的系统界面包名（如权限弹窗 `com.android.permissioncontroller`）；桌面会自动识别 |
| `safety.allow_text_input` / `max_text_length` | 开 / 30 | 能不能输入文字（搜索用）、一次最多几个字 |

## 互动

| 配置 | 默认值 | 说明 |
|---|---|---|
| `interaction.enabled` | 关 | 用手机上登录的她自己的账号点赞、评论、关注，见[互动](usage.md#互动) |
| `interaction.allow_like` / `allow_comment` / `allow_follow` | 开 | 三类可以单独关掉 |
| `interaction.daily_like_limit` / `daily_comment_limit` / `daily_follow_limit` | 30 / 5 / 3 | 每天最多点赞、评论、关注多少次 |
| `interaction.comment_max_length` | 50 | 一条评论最多多少个字 |

## 游玩

| 配置 | 默认值 | 说明 |
|---|---|---|
| `play.extra_persona` | 空 | 补充她的兴趣爱好，决定她搜什么、看什么、划走什么 |
| `play.step_interval_seconds` | 3 | 每步之间等待画面变化的秒数 |
| `play.max_session_minutes` / `max_steps_per_session` | 15 / 80 | 单次上限 |
| `play.daily_session_limit` / `daily_step_limit` | 8 / 600 | 每日上限；真正打开了应用才算玩了一次 |
| `play.max_model_calls_per_session` / `daily_model_call_limit` | 120 / 1000 | 一次、每天最多调用几次模型（失败的也算），用完就收尾 |
| `play.together_minutes` / `turn_wait_seconds` | 30 / 180 | 一起玩时最多玩多久、每次最多等对方多少秒 |
| `play.allow_chat_trigger` | 开 | 聊天中她能不能自己决定去玩手机（`play_phone` 工具） |
| `play.finish_notice` | `natural` | 玩完在聊天里怎么说：`natural` 她自己决定说不说、怎么说，`template` 固定通知，`off` 不说 |
| `play.fresh_start` | 开 | 每次开始先关掉应用再打开，从应用首页开始，不会接着上次停下的视频 |
| `play.go_home_after` | 开 | 玩完回到桌面，视频不会在后台一直播放 |
| `play.lock_after` | 开 | 玩完锁屏（iPhone 和安卓真机，模拟器不锁），下次玩时自动解开；设了锁屏密码就关掉，见[锁屏和解锁](devices.md#锁屏和解锁) |
| `play.skip_watched_videos` | 开 | 记住 30 天内看过的视频，不再点开、刷到了直接划走，见[看过的视频不再看](usage.md#看过的视频不再看) |
| `play.use_ui_tree` | 开 | 读取界面元素并编号，普通应用点击更准；读不到元素时只用坐标 |

## 自动玩

| 配置 | 默认值 | 说明 |
|---|---|---|
| `auto_play.enabled` | 关 | 空闲时自己决定要不要玩 |
| `auto_play.check_interval_minutes` | 30 | 每隔多少分钟考虑一次 |
| `auto_play.play_probability` | 0.3 | 每次考虑时去玩的概率 |
| `auto_play.active_hours` | `10:00-23:00` | 允许自动玩的时间段，可跨午夜；查得到麦生活的日程时按日程 |

## 生活联动

见[和麦生活联动](mai-life.md#配置)。

## 每个应用单独设置

`safety.allowed_apps` 里的每个应用除了 `package`、`name`、`note`（备注会告诉她这个应用能玩什么、怎么用），还可以写：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `step_interval_seconds` | `0` | 在这个应用里每步后等几秒；`0` 跟随全局 `play.step_interval_seconds`。看小说可以长一点，玩游戏短一点 |
| `max_minutes` | `0` | 这个应用最多玩几分钟；`0` 跟随全局。只能比全局短，填大了按全局算 |
| `reopen_on_start` | `"default"` | 打开时先关掉吗：`default` 按全局 `play.fresh_start`，`yes` 关掉重开，`no` 接着上次的画面 |
| `go_home_after` | `"default"` | 玩完回桌面吗：`default` 按全局 `play.go_home_after`，`yes` 回，`no` 留在这个应用 |
| `keep_open` | `false` | 旧选项，等于 `reopen_on_start = "no"` 加 `go_home_after = "no"`；单独写了那两项时以那两项为准 |
| `devices` | `[]` | 只在这些设备上用（写设备名）；不写就是哪台都能用，见[多台设备](devices.md#白名单按设备分) |

```toml
[[safety.allowed_apps]]
package = "com.tencent.ioshlddz"
name = "欢乐斗地主"
note = "和对方联机打 2v2"
reopen_on_start = "no"
go_home_after = "no"
step_interval_seconds = 1.5
```

- TOML 里没有「空值」，所以用 `0` 和 `"default"` 表示「跟随全局」。以前写了 `keep_open = true` 的配置不用改，行为和以前一样。
- 中途切到别的应用后，步间隔、回不回桌面按她当时实际在用的应用算；时长上限在开始时按第一个应用定下。

## 费用参考

每一步是一次带截图的视觉模型调用。实测 gpt-6.1-terra 每步约 5 秒、约 3700 tokens；她看视频、听歌时一次停 15～30 秒，平均每分钟不到 4 步，玩 10 分钟大约 35～40 步、十三四万 tokens。

想控制花费：调低 `play.daily_session_limit`、`max_session_minutes`，或者用 `play.daily_model_call_limit` 直接限制每天的模型调用次数。
