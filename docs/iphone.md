# 准备 iPhone

[← 返回 README](../README.md)

iPhone 通过 [WebDriverAgent](https://github.com/appium/WebDriverAgent)（WDA，苹果 XCTest 自动化的 HTTP 服务）控制：WDA 装在手机上，电脑用 [go-ios](https://github.com/danielpaulus/go-ios) 把它启动起来并转发到 `127.0.0.1:8100`，插件只和这个地址通信。

下面是**没有 Mac、用免费 Apple ID、Windows 不用管理员权限**的做法，在 iPhone 14 / iOS 18.6 + go-ios v1.3.2 + WDA v16 上实际走通过。同样强烈建议用闲置机。

| 步骤 | 做什么 | 多久做一次 |
|---|---|---|
| [1～3](#1-装驱动和-go-ios) | 装驱动、go-ios，签名安装 WDA | 一次（免费签名 7 天后重签） |
| [4～5](#4-手机设置) | 手机打开开发者模式、信任证书、改设置 | 一次 |
| [6](#6-启动-wda) | 启动隧道、挂镜像、启动 WDA、转发端口 | 每次开机 |
| [7](#7-填插件配置) | 填插件配置、换成 iPhone 的 Bundle ID | 一次 |

## 1. 装驱动和 go-ios

1. **装 Apple 的驱动**：装 iTunes 和 iCloud 的**网页安装版**（不要用微软商店的版本，Sideloadly 需要网页版）。苹果官网的 iCloud 下载按钮会跳到微软商店，可以用 [Sideloadly 下载页](https://sideloadly.io/#download) 里给的苹果官方链接。用数据线连上 iPhone，在手机上点「信任此电脑」。
   - 装完后 Windows 可能弹出「本地安全机构保护」拦截了 `mdnsNSP.dll`（Bonjour 的组件），不影响使用，点取消即可，**不要为此关掉 LSA 保护**。
2. **下载 go-ios**：从 [go-ios Releases](https://github.com/danielpaulus/go-ios/releases) 下载 `go-ios-win.zip`，解压得到 `ios.exe`（下面的命令都在它所在的目录里运行，或者把它放进 PATH）。`ios list` 能列出手机即可。

## 2. 做出 WDA 的安装包

从 [WebDriverAgent Releases](https://github.com/appium/WebDriverAgent/releases) 下载 `WebDriverAgentRunner-Runner.zip`（真机用的、未签名的版本），解压得到 `WebDriverAgentRunner-Runner.app`；新建一个叫 `Payload` 的文件夹，把 `.app` 放进去，把 `Payload` 文件夹压缩成 zip，再把扩展名改成 `.ipa`。

## 3. 签名并安装

用 [Sideloadly](https://sideloadly.io/) 选上一步的 `.ipa`，登录 Apple ID（建议专门注册一个，不用自己的主账号），点 Start 装到手机上。

- 免费 Apple ID 签的应用 **7 天后失效**，要重新签一次（Sideloadly 可以设置自动续签）。
- Sideloadly 会在 Bundle ID 后面加上你的开发者团队 ID，变成 `com.facebook.WebDriverAgentRunner.xctrunner.XXXXXXXXXX` 这样，第 6 步用 `ios apps` 查实际的。

## 4. 手机设置

- **开发者模式**：设置 → 隐私与安全性 → 开发者模式 → 打开，按提示重启（装过上一步的应用后才会出现这个开关）。
- **信任证书**：设置 → 通用 → VPN 与设备管理 → 点你的 Apple ID → 信任。

## 5. 让她玩得顺

- **不设锁屏密码**：WDA 解不了带密码的锁屏（见[锁屏和解锁](devices.md#锁屏和解锁)）。
- **自动锁定改成「永不」**：设置 → 显示与亮度 → 自动锁定，免得她看长视频时屏幕自己锁上。插件玩完会自己锁屏，下次玩时再解开。
- **打开勿扰或专注模式**：免得通知被截图发给模型。
- **白名单里的应用先手动打开一遍**，处理掉首次启动的弹窗。

## 6. 启动 WDA

每次开机后都要做。下面三个长期运行的命令各开一个终端窗口，别关。

**① 建立隧道**（iOS 17 及以上需要）：

```bash
ios tunnel start --userspace --pair-record-path=.\pair
```

- `--userspace` 让隧道不需要管理员权限（v1.3.2 要显式加上）；`--pair-record-path` 把和手机的配对记录固定存在 `pair` 文件夹里（不指定时直接散落在当前目录，换个目录运行就得重新配对）。第一次运行时手机上会问要不要信任这台电脑。
- 隧道日志里偶尔出现 `data plane stopped unexpectedly` 后会自己重连，刚插上手机的几秒内尤其常见，不用管。

**② 挂载开发者镜像**（每次手机重启后做一次；镜像会下载到 `devimages` 文件夹，下次直接用）：

```bash
ios image auto --basedir=.\devimages
```

iOS 17 起的开发者镜像要先找苹果的服务器签名。如果报 `x509: certificate signed by unknown authority`，是 Windows 里缺少苹果的根证书（公司电脑、长期没联网更新的电脑常见）：从 [Apple PKI](https://www.apple.com/certificateauthority/) 下载 **Apple Inc. Root** 证书，双击 → 安装证书 → 当前用户 → 「受信任的根证书颁发机构」，然后重新运行。

**③ 启动 WDA**。先查 WDA 的 Bundle ID（找以 `xctrunner` 开头的那个）：

```bash
ios apps
```

把 `<id>` 换成上面查到的；如果就是 `com.facebook.WebDriverAgentRunner.xctrunner`，三个参数都可以省略：

```bash
ios runwda --bundleid=<id> --testrunnerbundleid=<id> --xctestconfig=WebDriverAgentRunner.xctest
```

正常的话它会一直运行。如果几秒后就报 `lost connection to testmanagerd` 退出，先看隧道窗口和 `ios list` 手机还在不在（数据线松了、手机弹了提示都会断开），重新插好后再运行一次。

**④ 把手机上的 8100 端口转到电脑**：

```bash
ios forward 8100 8100
```

浏览器打开 `http://127.0.0.1:8100/status`，看到 `"ready" : true` 就好了。

> [!WARNING]
> WDA 在手机的 Wi-Fi 上也开着同样的端口（`/status` 里的 `ios.ip`），**没有密码**，同一个局域网里的人都能控制这台手机。只在信任的网络里用，不玩的时候关掉 `ios runwda`。

<details>
<summary>断线后自动重启 WDA 的 PowerShell 脚本</summary>

数据线松动、手机断开再插上后，`ios tunnel` 会自己重连，但 `ios runwda` 会退出，`ios forward` 也得重开（重新插上后手机在系统里的编号变了，旧的转发连不上）。可以把这两个命令放进一个脚本循环运行，`runwda` 退出后一起重启：

```powershell
while ($true) {
    $forward = Start-Process ios -ArgumentList "forward", "8100", "8100" -NoNewWindow -PassThru
    ios runwda --bundleid=<id> --testrunnerbundleid=<id> --xctestconfig=WebDriverAgentRunner.xctest
    Stop-Process -Id $forward.Id -Force
    Start-Sleep -Seconds 5
}
```

脚本里有中文时要存成「UTF-8 带 BOM」，否则 Windows 自带的 PowerShell 会按 GBK 读成乱码。

</details>

<details>
<summary>不用数据线：用 pymobiledevice3 通过 Wi-Fi 启动 WDA</summary>

go-ios 只能通过 USB 建隧道，一拔线 `ios runwda` 就会退出、WDA 跟着关掉。[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 能通过 Wi-Fi 建立同样的连接，而且不需要管理员权限（`--userspace`）：

```bash
uv venv pmd3 --python 3.12
uv pip install --python pmd3/Scripts/python.exe pymobiledevice3
# 插着线执行一次：允许已配对的电脑通过 Wi-Fi 连接（相当于 iTunes 的「通过 Wi-Fi 同步」，以后关掉换成 off）
pmd3/Scripts/pymobiledevice3 lockdown wifi-connections on
# 之后拔掉线也行：通过 Wi-Fi 找到手机、启动 WDA（--udid 填 ios list 里的编号，<id> 同上）
pmd3/Scripts/pymobiledevice3 developer dvt xcuitest <id> --userspace --mobdev2 --udid <udid>
```

- 这时 WDA 只在手机的 Wi-Fi 地址上（`pmd3/Scripts/pymobiledevice3 bonjour mobdev2` 能看到 `ip`），`device.wda_url` 填 `http://手机IP:8100`；或者在电脑上把 `127.0.0.1:8100` 转发到手机 IP，插件配置就不用改。
- 手机重启后开发者镜像会掉，用 `mounter mount-personalized` 加上 go-ios 下载好的镜像（`devimages` 里的 `.dmg`、`.trustcache`、`BuildManifest.plist`）重新挂上。
- 手机 IP 可能会变，最好在路由器里给它固定一个，或者关掉这个 Wi-Fi 的「私有无线局域网地址」。
- 实测拔线后截图、读界面、点击的速度和插着线时一样（截图约 0.15 秒）。

</details>

## 7. 填插件配置

| 配置 | 填什么 |
|---|---|
| `device.backend` | `ios_wda` |
| `device.wda_url` | 保持默认 `http://127.0.0.1:8100`（无线时见上面的折叠说明） |
| `safety.allowed_apps` | `package` 换成 iPhone 的 Bundle ID，以 `ios apps` 的输出为准 |

常见应用的 Bundle ID：

| 应用 | Bundle ID |
|---|---|
| 哔哩哔哩 | `tv.danmaku.bilianime`；外区 Apple ID 在 App Store 装到的 bilibili 是 `com.bilibili.inter`，内容一样 |
| 抖音 | `com.ss.iphone.ugc.Aweme` |
| 小红书 | `com.xingin.discover` |
| 网易云音乐 | `com.netease.cloudmusic` |
| 番茄小说 | `com.dragon.read` |
| Chrome | `com.google.chrome.ios` |

和安卓不一样的地方：

- iPhone 上查不出装了哪些应用（WDA 对没装的应用也只会说「没在运行」），所以开始玩时先当作白名单里的都装了；打开时发现没装，会在操作记录里写「手机上没有装…」，换一个重新决定，之后也不再选它（插件重新加载后重新记）。白名单最好只留手机上真装了的。
- 中文输入直接可用，不需要 ADBKeyboard，`device.text_input` 这项不用管。
- iPhone 没有返回键，她的「返回」是从屏幕左边缘往右滑，个别不支持这个手势的页面她会去点左上角的返回按钮。
- 第一次开始时会多等一两秒建立 WDA 会话。

然后回到 README 的[开始使用](../README.md#开始使用)，选好视觉模型就能试了。

## 常见问题

- **「连不上 iPhone 上的 WebDriverAgent」**：确认 `ios tunnel start`、`ios runwda`、`ios forward 8100 8100` 三个窗口都还开着（无线的话是 pymobiledevice3 那个窗口和转发），浏览器打开 `http://127.0.0.1:8100/status` 能看到 `ready`。
  - `ios runwda` 退出、报签名或证书错误，多半是免费签名过了 7 天，用 Sideloadly 重新签一次。
  - 开始时连不上会在 20 秒内重试，玩到一半短暂断开时会等最多 45 秒，恢复了就接着玩，WebUI「连接」里能看到在第几次重试、还剩多久。
  - 插件不会去重启你的 WDA、隧道或转发，它们退出了要你自己重新开。如果经常断开，换根数据线或换个 USB 口。
- **操作记录里写「手机上没有装…」**：要么真没装，要么 `package` 填的还是安卓包名，换成 `ios apps` 里看到的 Bundle ID。
- **`ios image auto` 报 `x509: certificate signed by unknown authority`**：Windows 缺少苹果根证书，见[第 6 步](#6-启动-wda)。
