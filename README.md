# 刷会儿手机（MaiBot 插件）

让 Bot 用一台安卓手机、模拟器或 iPhone **自己玩手机**：她会按自己的人设和心情决定打开哪个应用、想做什么，然后像真人一样看屏幕、点击、滑动、搜索。你可以在 WebUI 里实时看她在做什么，也可以在聊天里发 `/手机截图` 拿到当前画面。

> ⚠️ 请用闲置手机和专门注册的账号。可选的「互动」功能（默认关闭）打开后，她会用手机上登录的账号**公开**点赞、评论、关注，平台可能把这类自动化操作判定为异常而限流甚至封号，见「用她自己的账号互动」。

## 工作原理

```
决定玩什么（文本模型：人设 + 时间 + 最近玩过 + 白名单应用）
        ↓
循环：截图 → 检查前台应用是否在白名单 → 读取界面元素并在截图上编号
      → 视觉模型输出一个 JSON 动作 → 通过 adb（安卓）或 WebDriverAgent（iPhone）执行 → 等待画面变化
        ↓
玩够时间 / 达到步数 / 她自己说不玩了 / 被叫停 → 记录这次玩了什么
```

- 设备层是可替换的（`devices.py` 的 `PhoneDevice`）：安卓走 adb，iPhone 走 WebDriverAgent（`device.backend = "ios_wda"`）。
- 坐标使用 0~1000 的相对坐标，宿主为了适配模型缩放截图也不会错位；普通应用优先按元素编号点击，游戏、视频等读不到界面元素的画面只用坐标。

## 准备工作（安卓）

1. **安装 adb**（Android platform-tools），例如：

   ```bash
   winget install Google.PlatformTools
   ```

   装好后 `adb version` 能输出版本即可；不在 PATH 里时，在插件配置 `device.adb_path` 填 `adb.exe` 的完整路径。

   用 MuMu 模拟器 12 的话可以不装，直接用它自带的：`device.adb_path` 填 `<MuMu 安装目录>\nx_main\adb.exe`。

2. **准备一台给她用的手机**，强烈建议用独立的模拟器或备用机，不要用自己的主力手机：
   - 模拟器：MuMu、雷电、BlueStacks、Android Studio 模拟器都可以。在模拟器设置里打开 ADB 调试，把它显示的地址（如 `127.0.0.1:16384`，以模拟器里显示的为准）填到 `device.serial`。
     MuMu 开启 Hyper-V 时，模拟器有自己的局域网 IP（例如 `192.168.1.100:5555`），重启后可能变化，可以用 `<MuMu 安装目录>\nx_main\MuMuManager.exe info -v 0` 查看 `adb_host_ip` 和 `adb_port`。
     建议在 MuMu 多开器里**新建一个专门给她用的实例**，不要用装着自己游戏账号的那个。
   - 真机：开启开发者选项和 USB 调试，连上电脑后在手机上点「允许 USB 调试」。只连一台设备时 `serial` 可以留空。

   确认能看到设备：

   ```bash
   adb devices
   ```

3. **装好白名单里的应用，并手动打开一遍**，把首次启动的协议、权限、青少年模式等弹窗处理掉。建议用游客模式或小号，**不要登录重要账号**（她能操作这个账号能做的事）。

4. **（可选）中文输入**：搜索中文需要 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard)。安装 APK 后执行：

   ```bash
   adb shell ime enable com.android.adbkeyboard/.AdbIME
   ```

   ```bash
   adb shell ime set com.android.adbkeyboard/.AdbIME
   ```

   不装的话把 `device.text_input` 改成 `ascii_only`（只能输入英文和数字），或关闭 `safety.allow_text_input`。

5. **确认视觉模型**：`model.vision_task`（默认 `vlm`）用的模型必须能看图，点得准不准基本取决于它。可以用 `model.vision_model` 直接指定 model_config 里的某个模型（只影响本插件，不改 MaiBot 自己的 vlm 任务）。

   实测推荐 `gpt-6.1-terra`：点赞按钮、搜索框、评论输入框都点得准，会按人设和兴趣自己去搜、看完自己结束；iPhone 上 122 次调用平均每步 5 秒（中位 4.4 秒，最慢 18 秒），约 3700 tokens。换别的模型只要它能看图，改 `model.vision_model` 就行。

## 准备工作（iPhone）

iPhone 通过 [WebDriverAgent](https://github.com/appium/WebDriverAgent)（WDA，苹果 XCTest 自动化的 HTTP 服务）控制：WDA 装在手机上，电脑用 [go-ios](https://github.com/danielpaulus/go-ios) 把它启动起来并转发到 `127.0.0.1:8100`，插件只和这个地址通信。下面是**没有 Mac、用免费 Apple ID、Windows 不用管理员权限**的做法，在 iPhone 14 / iOS 18.6 + go-ios v1.3.2 + WDA v16 上实际走通过。同样强烈建议用闲置机。

1. **电脑上装 Apple 的驱动**：装 iTunes 和 iCloud 的**网页安装版**（不要用微软商店的版本，Sideloadly 需要网页版）。苹果官网的 iCloud 下载按钮会跳到微软商店，可以用 [Sideloadly 下载页](https://sideloadly.io/#download) 里给的苹果官方链接。用数据线连上 iPhone，在手机上点「信任此电脑」。
   - 装完后 Windows 可能弹出「本地安全机构保护」拦截了 `mdnsNSP.dll`（Bonjour 的组件），不影响使用，点取消即可，**不要为此关掉 LSA 保护**。

2. **下载 go-ios**：从 [go-ios Releases](https://github.com/danielpaulus/go-ios/releases) 下载 `go-ios-win.zip`，解压得到 `ios.exe`（下面的命令都在它所在的目录里运行，或者把它放进 PATH）。`ios list` 能列出手机即可。

3. **做出 WDA 的安装包**：从 [WebDriverAgent Releases](https://github.com/appium/WebDriverAgent/releases) 下载 `WebDriverAgentRunner-Runner.zip`（真机用的、未签名的版本），解压得到 `WebDriverAgentRunner-Runner.app`；新建一个叫 `Payload` 的文件夹，把 `.app` 放进去，把 `Payload` 文件夹压缩成 zip，再把扩展名改成 `.ipa`。

4. **用 [Sideloadly](https://sideloadly.io/) 签名并安装**：选上一步的 `.ipa`，登录 Apple ID（建议专门注册一个，不用自己的主账号），点 Start 装到手机上。
   - 免费 Apple ID 签的应用 **7 天后失效**，要重新签一次（Sideloadly 可以设置自动续签）。
   - Sideloadly 会在 Bundle ID 后面加上你的开发者团队 ID，变成 `com.facebook.WebDriverAgentRunner.xctrunner.XXXXXXXXXX` 这样，下面第 7 步用 `ios apps` 查实际的。

5. **手机上打开开发者模式并信任证书**：
   - 设置 → 隐私与安全性 → 开发者模式 → 打开，按提示重启（装过上一步的应用后才会出现这个开关）。
   - 设置 → 通用 → VPN 与设备管理 → 点你的 Apple ID → 信任。

6. **手机设置**：设置 → 显示与亮度 → 自动锁定 → **永不**（WDA 解不了带密码的锁屏，最好也不设锁屏密码）；打开勿扰或专注模式，免得通知被截图发给模型。白名单里的应用同样先手动打开一遍，处理掉首次启动的弹窗。

7. **在电脑上启动 WDA**（每次开机后都要做，下面三个长期运行的命令各开一个终端窗口，别关）。先建立隧道，iOS 17 及以上需要它：

   ```bash
   ios tunnel start --userspace --pair-record-path=.\pair
   ```

   - `--userspace` 让隧道不需要管理员权限（v1.3.2 要显式加上）；`--pair-record-path` 把和手机的配对记录固定存在 `pair` 文件夹里（不指定时直接散落在当前目录，换个目录运行就得重新配对）。第一次运行时手机上会问要不要信任这台电脑。
   - 隧道日志里偶尔出现 `data plane stopped unexpectedly` 后会自己重连，刚插上手机的几秒内尤其常见，不用管。

   然后挂载开发者镜像（每次手机重启后做一次；镜像会下载到 `devimages` 文件夹，下次直接用）：

   ```bash
   ios image auto --basedir=.\devimages
   ```

   iOS 17 起的开发者镜像要先找苹果的服务器签名。如果报 `x509: certificate signed by unknown authority`，是 Windows 里缺少苹果的根证书（公司电脑、长期没联网更新的电脑常见）：从 [Apple PKI](https://www.apple.com/certificateauthority/) 下载 **Apple Inc. Root** 证书，双击 → 安装证书 → 当前用户 → 「受信任的根证书颁发机构」，然后重新运行。

   查 WDA 的 Bundle ID（找以 `xctrunner` 开头的那个）：

   ```bash
   ios apps
   ```

   启动 WDA（把 `<id>` 换成上面查到的；如果就是 `com.facebook.WebDriverAgentRunner.xctrunner`，三个参数都可以省略）：

   ```bash
   ios runwda --bundleid=<id> --testrunnerbundleid=<id> --xctestconfig=WebDriverAgentRunner.xctest
   ```

   正常的话它会一直运行。如果几秒后就报 `lost connection to testmanagerd` 退出，先看隧道窗口和 `ios list` 手机还在不在（数据线松了、手机弹了提示都会断开），重新插好后再运行一次。

   把手机上的 8100 端口转到电脑：

   ```bash
   ios forward 8100 8100
   ```

   浏览器打开 `http://127.0.0.1:8100/status`，看到 `"ready" : true` 就好了。

   数据线松动、手机断开再插上后，`ios tunnel` 会自己重连，但 `ios runwda` 会退出，`ios forward` 也得重开（重新插上后手机在系统里的编号变了，旧的转发连不上）。嫌麻烦可以把这两个命令放进一个 PowerShell 脚本循环运行，`runwda` 退出后一起重启：

   ```powershell
   while ($true) {
       $forward = Start-Process ios -ArgumentList "forward", "8100", "8100" -NoNewWindow -PassThru
       ios runwda --bundleid=<id> --testrunnerbundleid=<id> --xctestconfig=WebDriverAgentRunner.xctest
       Stop-Process -Id $forward.Id -Force
       Start-Sleep -Seconds 5
   }
   ```

   脚本里有中文时要存成「UTF-8 带 BOM」，否则 Windows 自带的 PowerShell 会按 GBK 读成乱码。

   > WDA 在手机的 Wi-Fi 上也开着同样的端口（`/status` 里的 `ios.ip`），**没有密码**，同一个局域网里的人都能控制这台手机。只在信任的网络里用，不玩的时候关掉 `ios runwda`。

8. **插件配置**：`device.backend` 改成 `ios_wda`，`device.wda_url` 保持默认 `http://127.0.0.1:8100`。`safety.allowed_apps` 里的 `package` 要换成 iPhone 的 Bundle ID（和安卓包名不一样），以 `ios apps` 的输出为准，常见的有：

   | 应用 | Bundle ID |
   |---|---|
   | 哔哩哔哩 | `tv.danmaku.bilianime`；外区 Apple ID 在 App Store 装到的 bilibili 是 `com.bilibili.inter`，内容一样 |
   | 抖音 | `com.ss.iphone.ugc.Aweme` |
   | 小红书 | `com.xingin.discover` |
   | 网易云音乐 | `com.netease.cloudmusic` |
   | 番茄小说 | `com.dragon.read` |
   | Chrome | `com.google.chrome.ios` |

   - iPhone 上查不出装了哪些应用（WDA 对没装的应用也只会说「没在运行」），所以开始玩时先当作白名单里的都装了；打开时发现没装，会在操作记录里写「手机上没有装…」，换一个重新决定，之后也不再选它（插件重新加载后重新记）。所以白名单最好只留手机上真装了的。
   - 中文输入直接可用，不需要 ADBKeyboard，`device.text_input` 这项不用管。
   - iPhone 没有返回键，她的「返回」是从屏幕左边缘往右滑，个别不支持这个手势的页面她会去点左上角的返回按钮。

## 使用

### 直接跟她说（推荐）

不用记命令，像平时聊天一样说就行。她会结合上下文理解你的意思，自己决定要不要做、怎么回：

| 你说 | 她会 |
|---|---|
| 「去玩会儿手机吧」「帮我刷刷B站看看有没有睦睦的视频」 | 拿起手机，定好玩什么后顺口告诉你打算（`play_phone`） |
| 「你在看什么？给我看看」 | 截一张手机屏幕附在回复里发给你，边看边聊（`show_phone_screen`） |
| 「刚才在手机上看了啥？」 | 聊聊正在看或刚才看过的内容（`phone_activity`） |
| 「别玩了，过来陪我聊天」 | 放下手机接着聊（`stop_phone`） |
| 「来手机上一起玩 xx」 | 打开双人游戏和你在同一台手机上轮流玩（`play_phone` 的 `together`） |

- 通过聊天开始的那次，玩完后她会回到那个聊天，用自己的语气随口提两句看到的东西，也可能不说（`play.finish_notice`，可改为固定通知 `template` 或关闭 `off`）。
- 玩不成时她会照实说原因：手机没开机或连不上、今天玩够次数了、想做的事手机上的应用做不到（比如想玩的游戏没加进白名单）。这几种情况不占每天的次数，也不会再额外发结束通知；技术细节（adb 报错等）只显示在 WebUI 和 `/手机` 状态的「最近的错误」里。
- 看手机屏幕默认只有管理员可以，别人让她看时她会婉拒；开启 `plugin.screenshot_for_everyone` 后所有人都可以。聊天里是按宿主传来的发言人账号判断的，模型理论上能改写这个参数，所以只算辅助限制；`/手机截图` 命令的权限不受影响。
- 群里任何人都能叫她去玩手机，次数受每日额度限制；不想让她在聊天中去玩就关闭 `play.allow_chat_trigger`。
- 她回不回、什么时候回，还受 MaiBot 自身的回复频率设置影响；私聊消息少时可能会延迟处理。
- `play_phone`、`phone_activity`、`stop_phone` 一直放在她的工具列表里（不用先 tool_search，叫她去玩、问她在看什么时能马上反应），每次思考会多带几百 token 的工具说明；`show_phone_screen` 不常用，仍是需要时再搜索。

### 和她一起玩

同一台手机上两个人轮流玩的游戏（双人对战这类），可以和她一起玩：

1. 把游戏加进 `safety.allowed_apps`，备注里写清楚怎么玩。
2. 跟她说「来手机上一起玩 xx」。轮到你时她会用 `wait_turn` 等你。
3. 你在手机（或模拟器窗口）上操作。等你的时候插件每 1.5 秒截一次图比对，**不调用模型**；画面变了才轮到她看、做下一步。
4. 玩完她会放下手机，回到聊天里聊这次。`play.turn_wait_seconds`（默认 180 秒）没等到你操作时，她会自己决定接着等还是结束；一起玩最多 `play.together_minutes`（默认 30 分钟）。

聊天这边的她不知道手机上玩到哪了，所以开始时会提醒她别在聊天里编造进展，被问起时用 `phone_activity` 看看现在的情况。

### 聊天命令（精确控制）

| 命令 | 说明 | 权限 |
|---|---|---|
| `/手机` | 查看状态、今天玩了几次 | 所有人 |
| `/手机截图` | 发送当前手机画面 | 管理员；开启 `plugin.screenshot_for_everyone` 后所有人 |
| `/手机 开始 [想做的事]` | 去玩手机，例如 `/手机 开始 刷B站看猫猫`；不写则她自己决定。回复里会说她打算打开哪个应用，开始不了时说明原因 | 管理员 |
| `/手机 暂停` / `/手机 继续` / `/手机 停止` | 控制当前这次 | 管理员 |
| `/手机 帮助` | 查看命令 | 所有人 |

管理员在 `plugin.admin_user_ids` 中配置（填账号 ID，例如 QQ 号）；主程序的本地控制台始终视为管理员。没有权限时，回复里会附上发送者的账号 ID，方便复制到配置里。用命令开始的那次，结束后会在原聊天里说一声玩了什么。

WebUI 页面上的按钮不受 `admin_user_ids` 限制，能登录 WebUI 就能控制。

### 她自己决定

- **聊天中**：她自己无聊了也可能主动去玩（同样是 `play_phone`）。
- **空闲时**：开启 `auto_play.enabled` 后，每隔 `check_interval_minutes` 分钟，在 `active_hours` 时间段内按 `play_probability` 的概率自己去玩。
- **玩什么、看什么**：按 MaiBot 里的人设，加上 `play.extra_persona` 里写的兴趣爱好来定。没说要做什么时，她会想一个具体想看、想听的东西去搜（实测写了「喜欢吉他、种黄瓜、MyGO!!!!!」后，她会自己去网易云搜《栞》、在小红书搜阳台种黄瓜）；刷到不感兴趣的，短视频直接划走，长视频、笔记、文章返回列表换一个。人设里只写了性格、没写爱好时，挑内容会比较泛，建议在 `extra_persona` 里写几样她喜欢和不喜欢的。

### WebUI 实时观看

WebUI 侧边栏「插件扩展」→「手机实时画面」：约每秒刷新一次的屏幕画面、当前目标、她此刻的想法、每一步的操作记录，以及开始 / 暂停 / 继续 / 停止按钮。页面不在前台或取消勾选「实时刷新」时不会截图。

> 画面受 WebUI 单次查询 512 KiB 的限制，是压缩过的监控画面，不是视频流。想看流畅画面可以在电脑上另开 scrcpy。

### 用她自己的账号互动（点赞、评论、关注）

默认关闭。如果手机上登录的是**专门给她注册的账号**，可以打开 `interaction.enabled`，她看到真心喜欢的视频会点赞，有话想说会根据视频内容评论一两句，特别喜欢的博主、UP 主会关注。

- 评论会以这个账号**公开发出**。插件不会替她检查评论说得好不好，只拦下链接、@ 别人、长串数字（电话、QQ 号）和超长的评论；其余靠提示词要求她友好、不聊政治和敏感话题、不跟人争吵。
- 她不会发弹幕、私信，也不会回复别人的评论。评论的流程：她打开评论区，用 `comment` 指出评论输入框和要说的话，插件先点开输入框、等键盘弹出再写，并核对字真的进了输入框（iPhone 上；没写进去会告诉她，也不算额度），然后她自己点发送或发布。
- 每天的额度由代码限制（默认点赞 30 次、评论 5 条、关注 3 个），用完了就不能再做，每一步都会告诉她今天还剩多少。三类也可以单独关掉（`allow_like` / `allow_comment` / `allow_follow`）。
- 玩完在聊天里提起时，她知道自己这次点赞了几次、评论了什么、关注了谁；`phone_activity` 和 `/手机` 状态里也看得到。
- B 站、抖音都不欢迎自动化操作的账号，评论、关注太频繁容易被限流甚至封号，额度建议别调太高。账号资料里写明是 AI 角色会更坦诚，也更不容易被当成营销号。

## 安全边界

由**代码强制执行**（不依赖模型听话）：

- 只能打开 `safety.allowed_apps` 里、且手机上已安装的应用；每一步都会检查前台应用，停在白名单以外（桌面和 `extra_allowed_packages` 除外）就直接回桌面。
- 输入文字可整体关闭，并限制单次长度。
- 单次最多步数 / 分钟数、每天最多次数 / 步数、连续出错自动结束。
- 开启互动后：点赞、评论、关注每天的次数上限，评论的长度上限，评论里不能有链接、@ 和联系方式；没开启时 `like`、`comment`、`follow` 一律拒绝。
- 控制命令和截图命令有权限控制。

只在**提示词里要求**（模型可能违反）：不发弹幕、私信，不付款、不登录；没开启互动时也不发评论、不关注（她用普通的点击、输入也能做到，代码拦不住）。所以请务必使用独立设备和不重要的账号。

## 主要配置

| 配置 | 默认值 | 说明 |
|---|---|---|
| `device.backend` | `adb` | `adb`=安卓，`ios_wda`=iPhone |
| `device.serial` | 空 | 安卓设备序列号或 `IP:端口`；留空使用唯一已连接的设备 |
| `device.wda_url` | `http://127.0.0.1:8100` | iPhone 上 WebDriverAgent 的地址 |
| `device.text_input` | `adb_keyboard` | 安卓中文输入需要 ADBKeyboard；iPhone 不看这项 |
| `model.vision_task` / `vision_model` | `vlm` / 空 | 看屏幕做决定的模型 |
| `model.planner_task` | `utils` | 决定玩什么的文本模型 |
| `safety.allowed_apps` | 哔哩哔哩、抖音、小红书、网易云音乐、番茄免费小说 | 白名单 |
| `play.step_interval_seconds` | 3 | 每步之间等待画面变化的秒数 |
| `play.max_session_minutes` / `max_steps_per_session` | 15 / 80 | 单次上限 |
| `play.daily_session_limit` / `daily_step_limit` | 8 / 600 | 每日上限（每步一次识图调用）；真正打开了应用才算玩了一次 |
| `play.together_minutes` / `turn_wait_seconds` | 30 / 180 | 一起玩时最多玩多久、每次最多等对方多少秒 |
| `play.fresh_start` | 开 | 每次开始先关掉应用再打开，从应用首页开始，不会接着上次停下的视频 |
| `play.go_home_after` | 开 | 玩完回到桌面，视频不会在后台一直播放 |
| `interaction.enabled` | 关 | 用手机上登录的她自己的账号点赞、评论、关注，见「用她自己的账号互动」 |
| `interaction.daily_like_limit` / `daily_comment_limit` / `daily_follow_limit` | 30 / 5 / 3 | 每天最多点赞、评论、关注多少次 |
| `interaction.comment_max_length` | 50 | 一条评论最多多少个字 |
| `play.extra_persona` | 空 | 补充她的兴趣爱好，决定她搜什么、看什么、划走什么 |
| `auto_play.enabled` | 关 | 空闲时自己决定要不要玩 |

费用参考：每一步是一次带截图的视觉模型调用。实测 gpt-6.1-terra 每步约 5 秒、约 3700 tokens；她看视频、听歌时一次停 15～30 秒，平均每分钟不到 4 步，玩 10 分钟大约 35～40 步、十三四万 tokens。

## 测试

单元测试用假设备和脚本化的模型回复，不需要真实手机，在 MaiBot 根目录执行：

```bash
uv run pytest plugins/phone_play/tests -q
```

覆盖：动作解析与坐标换算、界面树解析（安卓和 iPhone，含被弹层和键盘挡住的元素）、adb 命令构造与错误提示、WebDriverAgent 请求（点与像素换算、会话失效重建、截图格式、锁屏、打开时才发现没装的应用）、完整的玩手机流程（选目标 → 打开应用 → 点击 / 滑动 / 结束）、白名单拦截、文字输入限制、互动的开关和额度（评论里的链接、@、联系方式）、连续出错结束、步数和每日额度、停止 / 暂停、WebUI 画面、命令权限与工具。

联调（接上模拟器后）：

1. `adb devices` 能看到设备，WebUI「手机实时画面」里能看到手机屏幕。
2. 私聊发 `/手机截图`，应收到当前画面。
3. 发 `/手机 开始 刷B站看猫猫`，在 WebUI 里看操作记录和她的想法；第一次建议盯着看几步，确认模型点得准。
4. 发 `/手机 停止`，应立即停下，并在聊天里收到这次玩了什么。
5. 在模拟器里手动切到「设置」，下一步应被自动带回桌面。

iPhone 联调：`http://127.0.0.1:8100/status` 显示 `ready` 后，按上面 2～5 步做一遍；第 5 步在 iPhone 上手动打开「设置」。iPhone 上第一次开始时会多等一两秒建立 WDA 会话。

## 常见问题

- **操作记录里每步都写「读不到界面元素（…退出码 137）」**：手机上有别的自动化工具（游戏脚本、按键精灵类应用）占着系统的界面自动化通道，日志里是 `UiAutomationService already registered`。关掉那个工具、换一个干净的实例，或把 `play.use_ui_tree` 设为 `false` 只用坐标。
- **搜索中文失败**：没装 ADBKeyboard 时把 `device.text_input` 设为 `ascii_only`，插件会提前告诉她只能用英文搜索。
- **想让她玩新装的应用**：只有 `safety.allowed_apps` 里的应用能打开。安卓用 `adb shell pm list packages -3`、iPhone 用 `ios apps` 找到包名或 Bundle ID，在插件配置里加一项应用，写上名称和备注（备注会告诉她这个应用能玩什么、怎么用）。
- **一直黑屏或转圈**：插件每步会比对截图，画面连续几步没变化时会提醒她换个做法或结束。如果某个应用在模拟器里总是加载不出来（常见于抖音这类会检测模拟器的应用），可以从白名单里去掉。
- **WebUI 显示「插件 WebUI 已下线」**：插件刚被重新加载（修改了插件文件或配置），等几秒后点页面右上角的「重载页面」。插件重新加载会结束正在进行的那次玩手机。
- **iPhone：「连不上 iPhone 上的 WebDriverAgent」**：确认 `ios tunnel start`、`ios runwda`、`ios forward 8100 8100` 三个窗口都还开着，浏览器打开 `http://127.0.0.1:8100/status` 能看到 `ready`。`ios runwda` 退出、报签名或证书错误，多半是免费签名过了 7 天，用 Sideloadly 重新签一次。玩到一半短暂断开时，插件会等最多 45 秒，恢复了就接着玩；如果经常断开，换根数据线或换个 USB 口。
- **iPhone：「iPhone 锁屏了」**：把自动锁定改成「永不」；设了锁屏密码的话插件解不开，只能手动解锁。
- **iPhone：操作记录里写「手机上没有装…」**：要么真没装，要么 `package` 填的还是安卓包名，换成 `ios apps` 里看到的 Bundle ID。
- **iPhone：`ios image auto` 报 `x509: certificate signed by unknown authority`**：Windows 缺少苹果根证书，见准备工作第 7 步。
- **连续出错结束**：看操作记录里失败那几步的原因；模型输出格式错误时，换一个更擅长遵循格式的视觉模型。

## 已知限制

- 快节奏游戏玩不了：每一步需要 3~10 秒（截图 + 识图 + 执行）。适合刷视频、逛帖子、听歌、看小说和休闲点击类游戏。
- `uiautomator dump` 在视频播放等画面不停变化时经常失败，这时只靠坐标点击，准确度取决于视觉模型。
- iPhone 上读界面元素时不让 WDA 判断元素是否可见（这一项能让一次读取从几秒变成零点几秒），插件只能按位置推断：键盘和盖在上面的列表挡住的会去掉，弹窗下面的按钮偶尔还会列出来；她同时看着截图，一般不影响。
- iPhone 上每次点击要将近 1 秒（WDA 模拟触摸本身的耗时），比安卓慢一些。
- iPhone 只按竖屏处理，横屏游戏的坐标可能对不上。
