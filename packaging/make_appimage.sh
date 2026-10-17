#!/usr/bin/env bash
# 把 PyInstaller 的 onedir 产物组装成 AppImage。
#
#   packaging/make_appimage.sh <onedir 目录> <输出 .AppImage 路径>
#
# 例：
#   packaging/make_appimage.sh dist/ncm-studio-linux-x86_64 dist/ncm-studio-linux-x86_64.AppImage
#
# 前置条件：先跑过 pyinstaller（NCMGUI_MODE=onedir），以及 scripts/fetch_ncmctl.py。
#
# ── 关于 runtime ────────────────────────────────────────────────────────────
# 不用 appimagetool 自带的 runtime，而是换成 uruntime。原因是 libfuse2：
#
# AppImage 的 runtime 自己就是负责挂载 squashfs 的那一层，它在启动早期
# dlopen("libfuse.so.2") —— 那一刻 payload 还没挂上，所以把 libfuse.so.2 作为
# 一个文件放进 AppDir 是够不着的，需要它时装着它的文件系统还读不到。
#
# uruntime 是静态链接的，自带 squashfuse，并且「FUSE 不可用时自动降级为解包
# 运行」。换掉 runtime，那个 dlopen 就根本不存在了，产物在用户机器上既不需要
# libfuse2，也不需要别的什么。
set -euo pipefail

ONEDIR=${1:?用法: make_appimage.sh <onedir 目录> <输出 .AppImage>}
OUTPUT=${2:?用法: make_appimage.sh <onedir 目录> <输出 .AppImage>}

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(dirname "$HERE")

APP_NAME=ncm-studio

# 钉死版本，不用滚动的 continuous —— 构建要可复现
# 注意 appimagetool 的 tag 没有 v 前缀（uruntime 的有），别照抄
APPIMAGETOOL_VERSION=1.9.1
URUNTIME_VERSION=v0.8.1
ARCH=x86_64
export ARCH

# ── 绕开 appimagetool 的「非 ASCII 路径」bug ────────────────────────────────
# appimagetool 的 argp 解析器会对**命令行参数**做字符集转换，参数里只要混进一个
# 非 ASCII 路径，它就直接以 "Invalid byte sequence in conversion input" 退出
# （中文 locale 下是「转换输入中有无效的字符序列」）。看着像参数写错了，其实是
# 它把自己坑了。
#
# 谁的项目目录会含中文？`~/桌面/...`、`~/下载/...`。这在国内基本是常态，所以
# 不能只在文档里写一句「请用英文路径」了事 —— 那等于让一大半人打不出包。
#
# 换个做法：把**组装目录、runtime 文件、输出文件**这三样挪到 /tmp 下的纯 ASCII
# 路径，打完再把产物拷回用户指定的位置。注意这三样都是要传给 appimagetool 的
# 参数，所以一个都不能漏。
#
# （显式覆盖 APPDIR / TOOLS_DIR 时请保证它们是 ASCII —— 这两项会绕过下面的判断。）
_needs_ascii_stage() {
    printf '%s' "$1" | LC_ALL=C grep -q '[^ -~]'
}

if _needs_ascii_stage "$ROOT$OUTPUT"; then
    echo "== 路径含非 ASCII 字符，改在临时目录里组装（appimagetool 的已知毛病）"
    STAGE=$(mktemp -d "${TMPDIR:-/tmp}/ncmgui-appimage.XXXXXX")
    trap 'rm -rf "$STAGE"' EXIT
    BUILD_ROOT="$STAGE/build"
    REAL_OUTPUT=$OUTPUT
    OUTPUT="$STAGE/$(basename "$OUTPUT")"
else
    BUILD_ROOT="$ROOT/build"
    REAL_OUTPUT=""
fi

APPDIR=${APPDIR:-"$BUILD_ROOT/$APP_NAME.AppDir"}
TOOLS_DIR=${TOOLS_DIR:-"$BUILD_ROOT/tools"}

ONEDIR_ABS=$(cd "$ONEDIR" && pwd)
mkdir -p "$TOOLS_DIR" "$(dirname "$OUTPUT")"

# ── 取工具 ──────────────────────────────────────────────────────────────────
appimagetool="$TOOLS_DIR/appimagetool"
if [ ! -x "$appimagetool" ]; then
    echo "== 下载 appimagetool $APPIMAGETOOL_VERSION"
    curl -fsSL --retry 3 -o "$appimagetool" \
        "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
    chmod +x "$appimagetool"
fi

runtime="$TOOLS_DIR/uruntime"
if [ ! -f "$runtime" ]; then
    echo "== 下载 uruntime $URUNTIME_VERSION（静态 runtime，代替需要 libfuse2 的默认 runtime）"
    curl -fsSL --retry 3 -o "$runtime" \
        "https://github.com/VHSgunzo/uruntime/releases/download/$URUNTIME_VERSION/uruntime-appimage-squashfs-x86_64"
fi

# ── 组装 AppDir ─────────────────────────────────────────────────────────────
echo "== 组装 AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# onedir 的启动器靠「自己所在目录」找同级 _internal/，所以整棵树必须一起搬，
# 不能只拷可执行文件，也不能在别处放软链接指过来
cp -r "$ONEDIR_ABS/." "$APPDIR/usr/bin/"

# 启动器叫什么取决于 NCMGUI_BUILD_NAME（CI 里带平台后缀，本地测试也常改），
# 但 AppDir 里应当统一叫 ncm-studio —— AppRun、.desktop、bundle_libs.py 三处
# 都按这个名字找它。不归一化的话，产物能打出来，运行时却是一句
# "exec: .../usr/bin/ncm-studio: not found"，而且只有真跑一次才发现。
#
# 重命名启动器本身是安全的：它靠自身所在目录定位同级 _internal/，跟文件名叫什么
# 无关（onedir 的启动器不是按 argv[0] 找资源的）。
launcher=""
for candidate in "$APPDIR/usr/bin"/*; do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
        launcher=$candidate
        break
    fi
done
if [ -z "$launcher" ]; then
    echo "错误：$APPDIR/usr/bin 下没找到可执行文件，onedir 产物不完整" >&2
    exit 1
fi
if [ "$(basename "$launcher")" != "$APP_NAME" ]; then
    echo "== 启动器改名：$(basename "$launcher") → $APP_NAME"
    mv "$launcher" "$APPDIR/usr/bin/$APP_NAME"
fi

install -m 0644 "$HERE/ncm-studio.png" "$APPDIR/$APP_NAME.png"
install -m 0644 "$HERE/ncm-studio.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
ln -sf "$APP_NAME.png" "$APPDIR/.DirIcon"

install -m 0644 "$HERE/ncm-studio.desktop" "$APPDIR/$APP_NAME.desktop"
install -m 0644 "$HERE/ncm-studio.desktop" "$APPDIR/usr/share/applications/$APP_NAME.desktop"

install -m 0755 "$HERE/AppRun" "$APPDIR/AppRun"

# ── 收系统库 ────────────────────────────────────────────────────────────────
echo "== 收集系统依赖"
python3 "$HERE/bundle_libs.py" "$APPDIR"

# ── 检查 glibc 下限 ─────────────────────────────────────────────────────────
# 收进来的系统库是在**构建机**上编译的，所以构建机越新，产物的实际门槛越高。
# 在 Ubuntu 24.04 上打的包到 Debian 12 上会直接 "GLIBC_2.39 not found" 起不来，
# 而构建、上传、发布全都不报错。这里提前卡住，别等到用户下载了才发现。
#
# 默认按项目声称的下限 2.35 卡。本机试玩、不打算分发时，用
# NCMGUI_MAX_GLIBC=<版本> 放行（比如本机是 Debian 13，就得放到 2.39 才过得去）。
MAX_GLIBC=${NCMGUI_MAX_GLIBC:-2.35}
echo "== 检查 glibc 下限（上限 $MAX_GLIBC）"
python3 "$HERE/check_glibc_floor.py" "$APPDIR" --max "$MAX_GLIBC"

# ── 打 AppImage ─────────────────────────────────────────────────────────────
echo "== 生成 AppImage"
# CI 上通常没有 FUSE，工具自身得先自解包才能跑
export APPIMAGE_EXTRACT_AND_RUN=1

"$appimagetool" \
    --runtime-file "$runtime" \
    --no-appstream \
    "$APPDIR" "$OUTPUT"

# 在临时目录里打的话，产物还没在用户要的位置
if [ -n "$REAL_OUTPUT" ]; then
    mkdir -p "$(dirname "$REAL_OUTPUT")"
    cp "$OUTPUT" "$REAL_OUTPUT"
    OUTPUT=$REAL_OUTPUT
fi

echo "== 完成：$OUTPUT"
ls -la "$OUTPUT"
