# 准备安卓手机

[← 返回 README](../README.md)

安卓手机和模拟器都通过 adb 控制。强烈建议用**独立的模拟器或备用机**，不要用自己的主力手机。

## 1. 安装 adb

```bash
winget install Google.PlatformTools
```

装好后 `adb version` 能输出版本即可。不在 PATH 里时，在插件配置 `device.adb_path` 填 `adb.exe` 的完整路径。

用 MuMu 模拟器 12 的话可以不装，直接用它自带的：`device.adb_path` 填 `<MuMu 安装目录>\nx_main\adb.exe`。

## 2. 连上手机

| 设备 | 怎么连 | `device.serial` 填什么 |
|---|---|---|
| 模拟器 | 模拟器设置里打开 ADB 调试 | 模拟器显示的地址，如 `127.0.0.1:16384` |
| 真机（数据线） | 开启开发者选项和 USB 调试，在手机上点「允许 USB 调试」 | 只连一台设备时留空 |
| 真机（无线调试，安卓 11+） | 开发者选项里打开「无线调试」 | 手机 IP 就行，如 `192.168.1.23`；也可以填 `IP:端口` |

确认能看到设备：

```bash
adb devices
```

**模拟器**

- MuMu、雷电、BlueStacks、Android Studio 模拟器都可以。
- MuMu 开启 Hyper-V 时，模拟器有自己的局域网 IP（例如 `192.168.1.100:5555`），重启后可能变化，可以用 `<MuMu 安装目录>\nx_main\MuMuManager.exe info -v 0` 查看 `adb_host_ip` 和 `adb_port`。
- 建议在 MuMu 多开器里**新建一个专门给她用的实例**，不要用装着自己游戏账号的那个。
- 模拟器不查锁屏、也不锁屏（见[锁屏和解锁](devices.md#锁屏和解锁)）。

**真机**

- 锁屏方式设成「无」或「滑动」：插件玩完会锁屏，下次玩时自己点亮、划掉锁屏界面；设了密码、图案、PIN 它解不开。

**无线调试**

- 端口是随机的，开关无线调试、插拔数据线、重启手机都会变。配置的端口连不上时，插件会直接问手机现在的端口（往手机 IP 的 5353 端口发一个 mDNS 查询）再连。
- 实测 OPPO Reno4 5G（安卓 12）：亮屏、息屏锁着时都能问到；端口变了之后拿旧端口去连，约 2.5 秒找到新端口；锁屏后 5 分钟不发命令，再连、解锁都正常。
- 手机页面上显示的端口有时不会马上刷新，以实际连上的为准。
- 这台电脑之前用 USB 授权过就不用配对；新电脑要先按手机上「使用配对码配对设备」执行一次 `adb pair`。
- 手机 IP 最好在路由器里固定下来。

## 3. 装好白名单里的应用

把要让她玩的应用装上，并**手动打开一遍**，处理掉首次启动的协议、权限、青少年模式、登录提示等弹窗。建议用游客模式或小号，**不要登录重要账号**（她能操作这个账号能做的事）。

默认白名单是哔哩哔哩、抖音、小红书、网易云音乐、番茄小说的安卓包名。想加别的应用，用下面的命令查包名，再在插件配置 `safety.allowed_apps` 里加一项：

```bash
adb shell pm list packages -3
```

## 4. 中文输入（可选）

搜索中文需要 [ADBKeyboard](https://github.com/senzhk/ADBKeyBoard)。安装 APK 后执行：

```bash
adb shell ime enable com.android.adbkeyboard/.AdbIME
```

```bash
adb shell ime set com.android.adbkeyboard/.AdbIME
```

不装的话把 `device.text_input` 改成 `ascii_only`（只能输入英文和数字，插件会提前告诉她），或关闭 `safety.allow_text_input`。

## 5. 填插件配置

| 配置 | 填什么 |
|---|---|
| `device.backend` | `adb` |
| `device.adb_path` | adb 在 PATH 里就写 `adb`，否则写完整路径 |
| `device.serial` | 见上面的表；只连一台时可以留空 |
| `device.text_input` | 装了 ADBKeyboard 用 `adb_keyboard`，没装用 `ascii_only` |

然后回到 README 的[开始使用](../README.md#开始使用)，选好视觉模型就能试了。

## 常见问题

- **每步都写「读不到界面元素（…退出码 137）」**：手机上有别的自动化工具（游戏脚本、按键精灵类应用）占着系统的界面自动化通道，日志里是 `UiAutomationService already registered`。关掉那个工具、换一个干净的实例，或把 `play.use_ui_tree` 设为 `false` 只用坐标。
- **「mDNS 也没找到它无线调试的新端口」**：手机上的「无线调试」关掉了，或者手机和电脑不在同一个 Wi-Fi。插拔数据线、开关无线调试后它可能自己关掉，回开发者选项里重新打开就行，端口不用改。
- **「要在手机上点允许」**：安卓上没点「允许 USB 调试」。
- **一直黑屏或转圈**：某个应用在模拟器里总是加载不出来（常见于抖音这类会检测模拟器的应用），可以从白名单里去掉。
