# ncmctl GUI

给 [`ncmctl`](https://github.com/chaunsin/netease-cloud-music) 套的 PyQt6 图形界面，支持 **Windows / Linux / macOS**。

`ncmctl` 本身功能很全，但纯命令行用起来门槛不低：参数散落在各个子命令里，输出目录要手敲，
二维码登录得盯着终端。这个程序把这些都收敛成侧边栏导航 + 表单 + 实时终端输出，
重点解决**输出文件夹的可视化选择**和**长任务 / 扫码登录的可视化反馈**。

![下载页](docs/screenshots/02-download.png)

## 功能覆盖

ncmctl 的全部子命令都在界面里有对应入口：

| 页面 | 对应命令 | 说明 |
| --- | --- | --- |
| 账号 | `login qrcode` / `phone` / `cookie` / `cookiecloud`、`logout` | 四种登录方式；扫码登录会在界面里显示二维码 |
| 下载 | `download` | 歌曲 ID、专辑 / 歌手 / 歌单链接；选音质、并发数、输出目录；**内置查重**与**歌词下载**，可一键搬空「我喜欢的音乐」 |
| NCM 解码 | `ncm` | 把 `.ncm` 还原成 `.mp3` / `.flac`，纯本地操作 |
| 云盘上传 | `cloud` | 上传单曲或整个文件夹，可按体积和正则过滤 |
| 账号任务 | `sign` / `scrobble` / `partner` / `task` | 单个任务立即执行，或挂成 cron 定时服务 |
| 监控代理 | `proxy` | 本机 HTTP(S) 抓包代理 |
| 调试工具 | `crypto encrypt` / `decrypt`、`curl` | 载荷加解密、直接调用导出的 Go API 方法 |
| 设置 | `update`、全局 `--home` / `-c` / `--debug` | ncmctl 定位、运行参数、自更新 |

## 安装

### 直接用打包好的程序

到 [Releases](https://github.com/iamlinxuhan/ncmctl-GUI/releases) 下载对应平台的文件：

| 平台 | 文件 |
| --- | --- |
| Windows | `ncmctl-gui-windows-x86_64.exe` |
| Linux | `ncmctl-gui-linux-x86_64.AppImage` |
| macOS | `ncmctl-gui-macos-arm64` |

都是单文件，双击即可，**不需要装 Python，也不需要单独装 ncmctl**。

- **Windows**：SmartScreen 可能提示「已保护你的电脑」，点「更多信息 → 仍要运行」。
- **macOS**：首次打开会被 Gatekeeper 拦下，右键点图标选「打开」，或在终端执行
  `xattr -d com.apple.quarantine ./ncmctl-gui-macos-arm64`。
- **Linux**：给执行位就行，不需要 FUSE，也不需要 libfuse2。

  ```bash
  chmod +x ncmctl-gui-linux-x86_64.AppImage
  ./ncmctl-gui-linux-x86_64.AppImage
  ```

  AppImage 自带全部依赖（含 Qt 用到的 xcb / xkbcommon / wayland 等系统库），
  只有显卡驱动（libGL / libEGL / libdrm）是用你机器上的 —— 那一份必须用宿主
  自己的，打包进去反而会把硬件加速顶掉。

  > **glibc 下限是 2.35**，对应 Ubuntu 22.04 / Debian 12 及以上。
  > 这由 PyQt6 的 manylinux 标签与构建机决定，打包格式换不掉。
  > CI 里有一道检查（`packaging/check_glibc_floor.py`）卡着这个数，
  > 免得哪天构建机升级后悄悄把老用户挡在门外。

### 从源码运行

需要 Python 3.10+。ncmctl 不用你操心，下面这行会按平台下载并校验：

```bash
git clone https://github.com/iamlinxuhan/ncmctl-GUI.git
cd ncmctl-GUI

python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/fetch_ncmctl.py     # 下载 ncmctl 到 vendor/
```

用 [uv](https://github.com/astral-sh/uv) 会快很多：

```bash
uv venv && uv pip install -r requirements.txt
```

Windows 上把 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。

> 虚拟环境是隔离的，看不到系统 `dist-packages` 里的 PyQt6。
> 如果你的发行版已经装了系统级 PyQt6 又不想重复下载，
> 可以改用 `python3 -m venv --system-site-packages .venv`。

## ncmctl 是内置的

界面里每个按钮都对应 ncmctl 的一个子命令，两者必须一起测、一起发。所以**版本是钉死的**：
本程序分发的是 **ncmctl v0.8.1**（MIT 许可，版权归 [chaunsin](https://github.com/chaunsin/netease-cloud-music) 所有，
许可证随产物一并分发）。升级本程序时它跟着一起升，你不用单独管。

查找顺序是：

1. 「设置」页里指定的路径
2. 随程序分发的那一份
3. `PATH`
4. 各平台常见安装目录

第 1 项优先级最高，是为了留个逃生口 —— 你想用自己那份（比如想跟上游新版本、
或者要打某个自己编译的补丁），在「设置」页填上完整路径即可，填了就以它为准。

常见安装目录包括 `~/go/bin`、`~/.local/bin`、`/usr/local/bin`、`/opt/homebrew/bin`，
Windows 上另有 `%USERPROFILE%\go\bin`、`%LOCALAPPDATA%\Microsoft\WinGet\Links`、
`%USERPROFILE%\scoop\shims` 和 Chocolatey 的安装目录。四处都没有时，启动会直接提示
「找不到 ncmctl」并告诉你去哪填，不会闷声失败。

### 内置那一份不能自更新

用内置版本时，「设置」页的「立即更新」是**灰的**，下面写着原因。这不是偷懒：

内置副本要么在 AppImage 的只读挂载里，要么在单文件包的临时解包目录里。
`ncmctl update` 想原地覆盖自己，前者必然失败，后者改了也会在退出时随临时目录一起丢掉。
与其让你点了之后收到一句莫名其妙的写入错误，不如直接说清楚：**内置版本随本程序一同更新**。

想自己更新 ncmctl，就在「设置」页指定一份你自己的，那一项不受此限制。

## 运行

```bash
./run.sh
# 或者
.venv/bin/python main.py
```

## 输出目录

下载、NCM 解码、crypto / curl 的 `-o` 都用同一个路径选择控件：

- **选择…** 打开系统文件对话框
- **打开** 在文件管理器里直接定位到该目录，方便确认结果
- **默认** 一键回到默认目录
- 路径不存在、类型不对或不可写时，输入框边框会变色并给出说明

默认目录取自系统音乐目录（Linux 上是 XDG 音乐目录，中文环境通常是 `~/音乐`；
macOS 是 `~/Music`；Windows 是 `%USERPROFILE%\Music`），在其下建 `ncmctl` 子目录，
避免和已有曲库混在一起。所有选择都会持久化到配置文件，下次打开自动恢复。

## 查重与「下载我喜欢的音乐」

这两项是 ncmctl 自己不提供、由界面补上的能力。

**查重。** ncmctl 遇到输出目录里的同名文件不会跳过，而是改写成 `歌曲(1).flac`
再下一遍。界面在下载前先把目标展开成歌曲清单，按 ncmctl 自己的命名规则
（`歌手 - 歌名`，非法字符替换为 `_`）算出每首歌的文件名，与输出目录比对，
把已有的剔掉，只把缺的交给 ncmctl。

- 比对只看文件名主干，`.mp3` / `.flac` 都算命中
- `歌曲(1).flac`、`歌曲(2).flac` 这类历史重复文件同样视为已存在
- 开关默认打开，在「下载设置」里可以关掉，关掉后行为与直接用 ncmctl 一致
- 「仅查重」按钮只出报告不下载，可以先看清楚会跳过哪些

**下载我喜欢的音乐。** 「快捷下载」里的一键按钮：读取账号的红心歌单
（按 `specialType == 5` 定位，不是 UID），展开全部曲目，套用上面的查重，
只下载缺的那些。歌单链接会回填到目标框，点完之后还能看到一共多少首、
已经跳过多少首。

> 红心歌单动辄几百首，但接口只会返回前 10 条 `tracks`，程序是拿全量
> `trackIds` 再分批（每 500 个一批）查详情补齐的。

## 歌词

**ncmctl 不写歌词，也不写任何标签。** 它的 `--tag` 帮助文本明写着 "reserved for
compatibility; download tag writing is not implemented"，实测下完的 flac 里
`format_tags` 是空的 —— 连 `artist` / `title` 都没有，歌名歌手全靠文件名。
所以歌词是界面补的（见 [ncmgui/lyrics.py](ncmgui/lyrics.py)）。

「下载设置」里的**歌词**是三选一，下载正常结束后自动接一轮：

| 选项 | 做什么 | 动你的文件吗 |
| --- | --- | --- |
| **独立 .lrc 文件**（默认） | 在音频旁边放一个同名的 `.lrc` | 不动，只多一个新文件 |
| 写入音频标签（内嵌） | 歌词写进音频文件自己的标签（flac 的 Vorbis 注释 / mp3 的 ID3 USLT / m4a / ogg / opus） | **会改写文件本身**，没有撤销 |
| 不下载歌词 | 什么都不做 | 不动 |

默认选 `.lrc` 是有意的：抓歌词要在下载之后再发几百个请求，万一失败或者某首歌
对不上，受影响的顶多是一个可以随手删掉的文本文件，而不是你刚下好的无损。

几个实现上的取舍：

- **外文歌自动带中文翻译。** 接口的 `tlyric` 自带时间轴，按时间戳逐行插在原文
  下面。原文 `[00:32.500]` 和译文 `[00:32.500]` 对齐得挺好；对不上的宁可丢掉 ——
  强行按顺序配会把译文错位到别的句子上，比"这句没翻译"难发现得多。
- **没有歌词的就跳过。** 接口在查无歌词时返回 `[00:00.00]暂无歌词`，在纯音乐上
  返回 `[00:05.00]纯音乐，请欣赏`。这两种都是"只有一行占位文字"，照着写出去只会
  让曲库里多一堆内容为"暂无歌词"的 .lrc，所以判定用的是"去掉时间戳后还剩多少字"
  而不是比对固定字符串。
- **时间戳统一写成两位小数。** LRC 的小数位本该是百分秒，接口给的是毫秒
  （`[00:44.870]`）。多数播放器认三位，但 **Elisa 25.04 的解析器把三位当成百分秒再
  ×10**，`[00:44.870]` 被算成 52.7 秒 —— 一句歌词能晚出 9 秒，误差在 0～9 秒之间
  乱跳，表现就是"这句都唱完了歌词还没翻过去"。两位小数是 LRC 的标准写法，新老播放器
  都算得对，代价只是毫秒被截到厘秒（最多偏早 9 毫秒）。
- **接口哪天改了写法，不会静默出错。** 时间戳怎么解析不是按位数写死的：小数位是"秒的
  小数部分"，`.5` / `.50` / `.500` 都是半秒，接口给到四位五位也照样算得对 —— 所以
  "网易换个精度"这半个坑已经堵上（另一半是输出端，见上一条）。真的碰上认不出的花样
  （比如哪天变成 `[00:44,870]`）时，那几行会被当成"没有时间戳的正文"，译文对不上、
  时间轴会乱，**却一句错都不报** —— 所以程序会主动查一遍并在下载日志里点出来
  （"认不出的时间戳 `[00:44,870]`"），宁可吵一句，也不交给你一份看着正常、实则错位的
  歌词。已经见过的一种变体是时间戳后面挂编号（`[00:00.00-1]`），这种认识，且**原样保留**
  —— 看不懂的东西不擅自删。
- **已经带歌词的不重复问接口。** `.lrc` 存在、或者标签里已有歌词，就直接跳过 ——
  重复跑一遍几百首的曲库不会白费几百个请求。但"已有歌词"要**连写法一起看**：早期版本
  写进去的是毫秒时间戳，那种文件会被认成"还得再写一遍"，重跑一次就**就地改过来**，
  不用删了重下。
- **文件名对不上就不写。** 歌词要落到哪个文件上，是**扫输出目录反查**出来的，
  不是拿歌名去拼后缀：同一首歌可能落成 `.flac` 也可能 `.mp3`，重名时还会变成
  `歌曲(1).flac`，三种都得认。

## VIP / 付费歌曲的标注

ncmctl 的下载日志只有进度条和一句 `report total: ...`，**从不提付费情况** ——
可拉不到音质的往往正是那几首。所以界面自己把 `fee` 字段带进来，标黄并加标记：

| 标记 | `fee` | 含义 |
| --- | --- | --- |
| `[VIP]` | 1 | 需会员才能听 / 下载所选音质 |
| `[付费专辑]` | 4 | 需先购买专辑 |
| `[VIP音质]` | 8 | 非会员只能听低音质 |

出现的位置：

- **下载进度行**，标记直接跟在歌名前面，整行标黄：

  ```
  [VIP音质]兰音Reine - 簪花记          [------------------↗     ] 23.06 MiB/25.83 MiB  89.25%
  ```

  这是最要紧的一处 —— 哪首歌卡住了、哪首位图失败，一眼就能对上号。

- **查重结果明细**里，每首歌名前面加标记（明细末尾附图例）
- **摘要行**加一句「待下载中 N 首需会员」
- **控制台的日志最前面**先列出这批里哪几首要会员

> ncmctl 的进度条只打文件名，不知道哪首需要会员。界面是在显示层把它对回
> 歌曲清单的，匹配时容忍两种差异：歌手之间的空格（`A,B` 与 `A, B`），以及
> pb 对超长标题的截断（`… - 宇宙一...` 仍能对回完整歌名）。

语气还会跟着账号的会员状态走 —— `GetUserInfo` 的 `vipType` 非 0 即视为会员：

- 是会员 → 「当前账号是会员，通常能下」
- 不是会员 → 「当前账号不是会员，多半会失败」
- 查不出来 → 不说满话，只提示「下载失败多半是它们」

> `fee` 的含义抄自 ncmctl 自己的注释（`api/weapi/song.go`）。未知的非 0
> 取值一律按 `[VIP]` 处理，宁可多标不可漏标。

## 平台说明

界面代码本身是跨平台的，麻烦的只有一处：**子进程的伪终端**。

`ncmctl download` 一进来就调 `pb.StartPool()`，那个函数会去问「我的标准输出
是不是一块真终端」。不是的话它直接报错退出，**一个字节都下不了** ——
所以不能像普通程序那样给子进程接管道。

| 平台 | 方案 |
| --- | --- |
| Linux / macOS | 标准库 `pty.openpty()`，配合 `start_new_session=True` 让 `/dev/tty` 打不开，从而退回到程序给的 pty |
| Windows | [`pywinpty`](https://github.com/andfoy/pywinpty)（ConPTY，需要 Windows 10 1809+） |

Windows 上 `pywinpty` 已经写进 `requirements.txt` 的条件依赖，打包时会连同它
自带的 `conpty.dll` / `OpenConsole.exe` 一起进 exe，用户不需要额外装任何东西。
从源码跑的话 `pip install -r requirements.txt` 会自动带上。

> 注意「用 `cmd` / `powershell` 包一层」解决不了这个问题：包装进程自己的
> stdout 也是管道，它拉起的 ncmctl 继承的还是那条管道。

## 关于安全

- 本程序**不保存**你的账号密码。登录凭据由 ncmctl 自己写在 `~/.ncmctl/` 下，本程序只读取状态用于显示。
- 密码、CookieCloud 密码这类值会以命令行参数的形式传给 ncmctl，因而会出现在进程列表里 —— 这是 ncmctl 自身的接口限制。**介意的话请用扫码登录**，它不需要把任何凭据交给本程序。
- 粘贴的 Cookie 会先写入一个只有本人可读（POSIX 下 `0600`）的临时文件再传给 `--file`，避免直接出现在进程参数中，用完即删。
- 登出、云盘上传、听歌任务、音乐合伙人、自更新这些会改动账号状态或不可逆的操作，执行前都有二次确认。

## 开发者说明

```bash
# 命令构建层的自检（纯函数，不需要 GUI）
.venv/bin/python -m ncmgui.ncmctl

# 目标解析 / 查重层的自检（纯函数，不需要 GUI、不联网）
.venv/bin/python -m ncmgui.resolver

# 歌词处理的自检（纯函数，不联网；需要 mutagen 才能验标签往返）
.venv/bin/python -m ncmgui.lyrics

# 无头冒烟测试：构建整个界面、逐页实例化、跑一遍控制台逻辑
.venv/bin/python main.py --self-test

# 把每个页面渲染成 PNG（不依赖 X11 抓屏工具，Wayland 下也能用）
.venv/bin/python main.py --screenshot ./docs/screenshots

# 下载页的端到端集成测试。会真的调接口、真的下歌，需要已登录。
# 用临时目录当输出目录和配置，不动你的真实曲库与配置。
.venv/bin/python -u tests/gui_integration.py
```

### 自己打包

```bash
python scripts/fetch_ncmctl.py          # 先把 ncmctl 取到 vendor/
pip install pyinstaller

# Windows / macOS：单文件
NCMGUI_BUNDLE_NCMCTL=1 pyinstaller ncmctl-gui.spec --noconfirm

# Linux：onedir + AppImage
NCMGUI_MODE=onedir NCMGUI_BUNDLE_NCMCTL=1 \
  pyinstaller ncmctl-gui.spec --noconfirm
bash packaging/make_appimage.sh \
  dist/ncmctl-gui dist/ncmctl-gui-linux-x86_64.AppImage
```

`NCMGUI_BUNDLE_NCMCTL=1` 表示「这个产物必须有 ncmctl」，缺了直接报错而不是警告一下
照常产出 —— 一个不含 ncmctl 的包发出去，用户那边是每个功能都用不了。

Linux 之所以是 onedir 而不是单文件，是因为 AppImage 必须建在目录树上：单文件包在
运行时还会把自己再解包一次，套进 AppImage 就成了双重解包，启动慢且白占一份临时空间。

CI（`.github/workflows/build.yaml`）在推 `main` 时会三平台各打一次包（只出产物、
不发 Release），用来尽早暴露某个平台特有的构建问题；打 `v*` tag 时才额外发 Release。

CI 里有三类断言，都是同一个思路 —— **「打包成功」不等于「东西真的进去了」**：

| 断言 | 防的是什么 |
| --- | --- |
| 清点 `conpty.dll` / `OpenConsole.exe` 等（Windows） | pywinpty 的非 `.pyd` 二进制不在依赖分析里，漏了不报错，只在用户那边变成「一下载就失败」 |
| 清点内置的 `ncmctl`（三平台） | 同上，缺了的话整个界面一个功能都用不了 |
| `check_glibc_floor.py`（Linux） | 构建机越新，收进去的系统库要求越高。在 Ubuntu 24.04 上打的包到 Debian 12 上会 `GLIBC_2.39 not found`，而构建、上传、发布全都不报错 |

最后一条是最容易忽略的：PyInstaller 会把**构建机**的 libglib、libgtk、libstdc++、
libxcb 一起收进产物，这些库在构建机的 glibc 上编译，于是产物的实际门槛被构建机决定，
和 PyQt6 自己的 manylinux 标签没关系。所以 CI 里 Linux 任务**固定跑在 `ubuntu-22.04`
上**，不要图省事改成 `ubuntu-latest` —— 那是目前唯一能同时满足 PyQt6（≥2.34）和
Debian 12 / Ubuntu 22.04 用户（≤2.35）的档位。

### 代码结构

```
main.py                入口，含 --self-test / --screenshot
ncmctl-gui.spec        PyInstaller 打包配置（三平台共用）
scripts/
  fetch_ncmctl.py      下载并校验 ncmctl 到 vendor/（版本与 SHA-256 钉在里面）
  make_icon.py         生成图标（纯几何绘制，不依赖字体）
packaging/
  ncmctl-gui.png/.ico  图标，已入库（免得构建期还要装 Pillow）
  ncmctl-gui.desktop   Linux 桌面项
  AppRun               AppImage 入口
  bundle_libs.py       把 Qt 需要的系统库收进 AppDir
  check_glibc_floor.py 检查产物真正能跑的最低 glibc，超线即失败
  make_appimage.sh     组装 AppDir + 打 AppImage
tests/
  gui_integration.py   下载页端到端测试（真实调接口、真实下载）
ncmgui/
  resources.py         包内资源定位（按 __file__，onefile / onedir 都成立）
  ncmctl.py            命令构建：把界面选择翻译成参数数组（不依赖 Qt）
  resolver.py          目标解析与查重：展开 id/链接 → 歌曲清单 → 比对输出目录（不依赖 Qt）
  lyrics.py            歌词：解析 LRC、合并翻译、写 .lrc 或写进音频标签（不依赖 Qt）
  runner.py            subprocess + pty 封装，流式输出为信号（POSIX / Windows 双后端）
  workers.py           后台任务（守护线程），用于不阻塞界面的解析
  config.py            配置持久化
  theme.py             调色板、全局 QSS、霓虹辉光
  window.py            侧边栏 + 页面栈 + 底部作业控制台
  widgets/             Card、PathPicker、PathList、FormCard、ConsolePanel
  pages/               八个功能页
```

几条设计约定：

1. **命令一律以参数数组传递，绝不拼 shell 字符串。** 路径里的空格、中文、引号都不需要转义，也不存在注入问题。
2. **命令构建层不依赖 Qt。** `ncmgui/ncmctl.py` 和 `ncmgui/resolver.py` 都是纯函数集合，可以脱离界面单独测试，见上面的自检命令。
3. **子进程跑在伪终端上，不是管道。** 原因见上面的「平台说明」。另外 pty 尺寸必须
   用 `TIOCSWINSZ` / ConPTY 的 `dimensions` 报成正常行列数 —— 默认的 0×0 会让
   进度条库算出宽度 0，整条进度渲染成空白。
4. **pty 输出要按增量方式解码。** UTF-8 是变长编码，而 `os.read` 按字节返回，
   一个汉字正好跨在读取边界上时两半会被分开解码，日志里就出现 `��`。用
   `codecs.getincrementaldecoder` 把没凑齐的字节留到下一轮。

## 许可

本程序是 ncmctl 的第三方前端。ncmctl 本身以 MIT 许可发布，版权归 chaunsin 所有。
它的许可证与 README 会随产物一起分发（打包后在 `ncmgui/assets/ncmctl/` 下），
这是分发第三方二进制应尽的义务，不是可有可无的点缀。

本项目以 GPL-2.0 发布，见 [LICENSE](LICENSE)。

「写入音频标签」这一种歌词模式用到 [mutagen](https://mutagen.readthedocs.io/)，它同样以
GPL-2.0-or-later 发布，向后兼容。默认的「独立 .lrc 文件」不需要它。

作者：iamlinxuhan &lt;2276677131@qq.com&gt;
