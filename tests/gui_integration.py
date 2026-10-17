"""下载页的端到端集成测试。

走的是页面真实代码路径（``_download_liked`` / ``_start`` / ``_preview``），
不 mock 解析层，所以会真的调网易云接口、真的下歌。需要已登录。

    .venv/bin/python -u tests/gui_integration.py

用临时目录当输出目录、临时文件当配置文件，不碰你的真实配置和曲库。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QMessageBox

# 无头环境下模态框没人点，会永久阻塞 —— 换成记录
DIALOGS: list[tuple[str, str, str]] = []
for _name in ("warning", "information", "critical", "question"):

    def _stub(parent, title, text, *a, _n=_name, **kw):
        DIALOGS.append((_n, title, text))
        print(f"      [对话框 {_n}] {title}: {text[:120]}")
        return QMessageBox.StandardButton.Ok

    setattr(QMessageBox, _name, staticmethod(_stub))

import mutagen

from ncmgui import ncmctl, theme
from ncmgui.config import Config
from ncmgui.pages.download import DownloadPage
from ncmgui.resolver import PLAYLIST_URL, Plan, Resolver, Song
from ncmgui.widgets.form import set_combo_data
from ncmgui.window import MainWindow

app = QApplication([])
theme.apply_theme(app)

cfg_path = Path(tempfile.mkdtemp(prefix="ncmgui-cfg-")) / "config.json"
window = MainWindow(Config(cfg_path))
window.show()
window.show_page("download")
page = window.pages["download"]

fails: list[str] = []


def check(name: str, cond: object, detail: object = "") -> None:
    print(("  OK  " if cond else "  FAIL") + f" {name} {detail}", flush=True)
    if not cond:
        fails.append(name)


def pump(pred, seconds: float = 240) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.05)
        if pred():
            return True
    return False


def positionals(argv: list[str]) -> list[str]:
    """取 argparse 的位置参数：跳过 argv[0] 与所有 --flag（含其取值）。"""
    out, expecting_value = [], False
    for token in argv[1:]:
        if expecting_value:
            expecting_value = False
            continue
        if token.startswith("-"):
            expecting_value = True
            continue
        out.append(token)
    return out


submitted: list[tuple[str, list[str], list[str]]] = []
tagger_seen: list[object] = []
hooks_seen: list[object] = []
real_submit = window.submit


def spy(title, argv, confirm=None, cwd=None, intro=None, tagger=None, on_finish=None):
    submitted.append((title, list(argv), list(intro or [])))
    tagger_seen.append(tagger)
    hooks_seen.append(on_finish)
    return real_submit(
        title, argv, confirm, cwd, intro=intro, tagger=tagger, on_finish=on_finish
    )


window.submit = spy

# ====================================================================== #
print("[0] 解析红心歌单，把除前 2 首外的都伪造成本地已有文件", flush=True)
out = Path(tempfile.mkdtemp(prefix="ncmgui-out-"))
resolver = Resolver(ncmctl.find_binary(), opts=ncmctl.GlobalOpts())
playlist_id, playlist_name = resolver.liked_playlist()
plan = resolver.plan([PLAYLIST_URL.format(playlist_id)], out)
print(f"      歌单「{playlist_name}」共 {plan.total} 首；账号会员={plan.vip}", flush=True)
print(f"      其中需会员/购买 {len(plan.paid)} 首", flush=True)
keep = plan.songs[:2]
for song in plan.songs[2:]:
    (out / f"{song.base}.flac").write_bytes(b"")
print(f"      伪造 {plan.total - 2} 个文件，保留 {len(keep)} 首待下载", flush=True)

page.output.set_path(str(out))
page.skip_existing.setChecked(True)

# ====================================================================== #
print("[1] 点击「下载我喜欢的音乐」", flush=True)
page._download_liked()
ok = pump(lambda: page._task is not None and page._task.finished)
check("解析任务完成", ok, "" if ok else f"对话框={DIALOGS}")
pump(lambda: bool(submitted), 15)

check(
    "回填了歌单链接",
    PLAYLIST_URL.format(playlist_id) in page.targets.toPlainText(),
    page.targets.toPlainText()[:60],
)
check("提交了一个下载作业", len(submitted) == 1, [t for t, _, _ in submitted])

intro: list[str] = []
if submitted:
    title, argv, intro = submitted[0]
    check("标题带歌单名", playlist_name in title, title)
    check("只提交了缺的 2 首", sorted(positionals(argv)) == sorted(str(s.id) for s in keep), positionals(argv))
    check("音质参数正确", argv[argv.index("--level") + 1] == "lossless")
    check("输出目录正确", argv[argv.index("--output") + 1] == str(out))

check("报告显示跳过数", f"已存在 {plan.total - 2} 首" in page.report_label.text(),
      page.report_label.text())
check("「显示明细」按钮出现", page.details_button.isVisible())
check("明细默认收起", not page.report_view.isVisible())
page._toggle_details()
check("展开后可见且带内容",
      page.report_view.isVisible()
      and f"已存在，跳过下载（{plan.total - 2} 首）" in page.report_view.toPlainText(),
      page.details_button.text())
check("按钮文案翻转", page.details_button.text() == "隐藏明细")
page._toggle_details()
check("收起后重新隐藏", not page.report_view.isVisible())

# ---- VIP 标注 -------------------------------------------------------- #
print("      —— VIP 标注 ——", flush=True)
detail = page.report_view.toPlainText()
# 注意用页面自己的 _plan —— 上面那个 plan 是伪造文件之前算的，missing 还是全量
shown_plan = page._plan
paid_songs = [s for s in shown_plan.missing if s.is_paid]
if paid_songs:
    # 待下载的这 2 首里若有付费的，明细里就该带方括号标记
    missing_marks = [s for s in paid_songs if f"[{s.fee_label}] {s.display}" not in detail]
    check("待下载的付费歌曲在明细里带标记", not missing_marks,
          [(s.display, s.fee_label) for s in missing_marks])
    check("明细里有图例", "标记含义：" in detail)
    check("图例说明当前会员状态",
          ("当前账号是会员" if plan.vip else "当前账号不是会员") in detail, plan.vip)
else:
    print("      （这 2 首都不付费，标记相关断言跳过）", flush=True)

check("控制台开场白非空（有付费歌曲时）", bool(intro) == bool(paid_songs), intro[:1])

# ---- 进度行标注 ------------------------------------------------------ #
# 开场白只在开跑前出现一次，滚两屏就没了。真正要的是下载过程中每一行进度
# 都带着标记 —— 那是 FeeTagger 干的活，这里离线验一遍，真机结果在 [2] 里查。
print("      —— 进度行标注 ——", flush=True)
tagger = tagger_seen[0] if tagger_seen else None
check("作业装上了标注器", tagger is not None, tagger_seen[:1])

if tagger is not None:
    for song in shown_plan.missing:
        line = f"{song.base}                      [--------] 1.00 MiB/2.00 MiB  50.00%"
        got = tagger(line)
        if song.is_paid:
            check(f"付费进度行加标记：{song.display[:24]}",
                  got is not None and got.startswith(f"[{song.fee_label}]"), got)
        else:
            check(f"免费进度行不动：{song.display[:24]}", got is None, got)

# ====================================================================== #
print("[2] 真机下载这 2 首，验证查重与实际落盘一致", flush=True)
if submitted:
    end = time.time() + 400
    ticks = 0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.1)
        ticks += 1
        if not window._running_panels():
            break

    panel = window.console_tabs.widget(window.console_tabs.count() - 1)
    log = panel.view.toPlainText()
    report_line = next(
        (ln for ln in log.splitlines() if ln.startswith("report total:")), "(没有 report 行)"
    )
    print(f"      等待循环跑了 {ticks} 次（{ticks / 10:.1f} 秒）；{report_line}", flush=True)

    names = sorted(f.name for f in out.iterdir() if f.suffix in (".mp3", ".flac"))
    real = [n for n in names if (out / n).stat().st_size > 0]
    check("只新增了 2 个真实音频文件", len(real) == 2, real)
    check("落盘文件名与查重推断一致", {Path(n).stem for n in real} == {s.base for s in keep})

    plan2 = resolver.plan([PLAYLIST_URL.format(playlist_id)], out)
    check("再查重 => 一首不缺", not plan2.missing, plan2.report())

    # 开场白应当真的写进了控制台，而不只是传了个参数
    if intro:
        check("开场白已写进控制台", intro[0] in log, log.splitlines()[:3])

    # 真机跑出来的进度行应当自带标记 —— 这是「下载中也能看见」的最终证据
    if any(song.is_paid for song in keep):
        marked = [ln for ln in log.splitlines() if ln.startswith("[VIP")]
        check("下载日志里有带标记的进度行", bool(marked), marked[:2])
        check(
            "标记跟着歌名而不是另起一行",
            all(ln.split(" ")[0].startswith("[") for ln in marked),
            marked[:1],
        )
    else:
        print("      （这 2 首都不付费，真机标记断言跳过）", flush=True)

    # ---- 歌词：默认的「独立 .lrc 文件」 -------------------------------- #
    # 歌词是下载结束之后才接上去的一轮，走的是 jobFinished → on_finish 这条路，
    # 所以这里要等的是页面自己那个后台任务，而不是 ncmctl 那个作业面板。
    print("      —— 歌词（独立 .lrc）——", flush=True)
    check("作业装了歌词后处理", hooks_seen and hooks_seen[-1] is not None, hooks_seen[-1:])
    ok = pump(lambda: "── 歌词：" in panel.view.toPlainText(), 240)
    check("歌词这一轮跑完了", ok, panel.view.toPlainText()[-200:])
    print("      " + next(
        (ln for ln in panel.view.toPlainText().splitlines() if ln.startswith("── 歌词：")),
        "(没有歌词汇总行)",
    ), flush=True)

    audio = sorted(f for f in out.iterdir() if f.suffix in (".mp3", ".flac") and f.stat().st_size > 0)
    lrcs = sorted(out.glob("*.lrc"))
    check("每首下好的歌都有同名 .lrc", {p.stem for p in lrcs} == {p.stem for p in audio},
          [p.name for p in lrcs])
    if lrcs:
        body = lrcs[0].read_text("utf-8")
        check(".lrc 里有时间戳", "[00:" in body, body[:80])
        check(".lrc 不是占位串", "暂无歌词" not in body and "纯音乐，请欣赏" not in body,
              body[:80])
        print(f"      {lrcs[0].name} 前两行：{body.splitlines()[:2]}", flush=True)

    # ---- 封面：默认的「保存为同名图片」 -------------------------------- #
    # 封面这一轮排在歌词前面（先图后字），所以走到这里时已经落盘了。
    print("      —— 封面（同名图片）——", flush=True)
    art = sorted(
        p for p in out.iterdir()
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and p.stat().st_size
    )
    check("每首下好的歌都有同名封面图", {p.stem for p in art} == {p.stem for p in audio},
          [p.name for p in art])
    for path in art:
        head = path.read_bytes()[:12]
        real_image = (
            head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n"
        )
        check(f"封面确实是图片：{path.name}", real_image, head)

# ====================================================================== #
print("[3] 关掉查重后，应当把原始目标直接交给 ncmctl", flush=True)
submitted.clear()
page._plan = None
page.skip_existing.setChecked(False)
page.targets.setPlainText("1901371647\n2161154646")
page._start()
pump(lambda: bool(submitted), 10)
check("提交了作业", len(submitted) == 1)
if submitted:
    argv = submitted[0][1]
    assert argv[0] == "download", argv
    check("原始目标原样传递", positionals(argv) == ["1901371647", "2161154646"], argv)

# 这一步是真下载，等它连同后面的歌词一起收尾 —— 它的输出目录和 [6] 是同一个，
# 不等干净的话两边会抢同一批文件（老版本这里没等，因为那时没有后处理）
loose_panel = window.console_tabs.widget(window.console_tabs.count() - 1)
pump(lambda: not window._running_panels(), 400)
pump(lambda: "── 歌词：" in loose_panel.view.toPlainText(), 240)
loose_log = loose_panel.view.toPlainText()
check("关掉查重时也会抓歌词（解析只为取 ID，下载目标没被换掉）",
      "── 歌词：" in loose_log,
      [ln for ln in loose_log.splitlines() if ln.startswith("── 歌词：")])
loose_lrc = {p.stem for p in out.glob("*.lrc")}
for song in page._plan.songs if page._plan else []:
    # 这两首是按 ID 下的，歌词该按 ncmctl 实际落盘的文件名写出去
    check(f"按 ID 下载的歌也配上了歌词：{song.display[:20]}", song.base in loose_lrc, sorted(loose_lrc))

# ====================================================================== #
print("[4] 「仅查重」不应当触发下载", flush=True)
submitted.clear()
page._plan = None
page.skip_existing.setChecked(True)
page.output.set_path(str(out))
page.targets.setPlainText(PLAYLIST_URL.format(playlist_id))
page._preview()
pump(lambda: page._task is not None and page._task.finished, 240)
pump(lambda: page.report_label.text().startswith("共"), 5)  # 等队列里的 done 信号落地
check("没有提交下载作业", not submitted, submitted)
check("有查重结论", page.report_label.text().startswith("共"), page.report_label.text())

# ====================================================================== #
# 会员语气：离线构造，三种状态都要说对话
print("[5] 会员状态影响文案（离线）", flush=True)
free = Song(1, "免费歌", ("甲",))
vip = Song(2, "会员歌", ("乙",), fee=1)


def paid_plan(vip_state: bool | None) -> Plan:
    return Plan(songs=[free, vip], missing=[vip], present=[free], vip=vip_state)


for state, expect, forbid in (
    (True, "通常能下", "多半会失败"),
    (False, "多半会失败", "通常能下"),
    (None, "多半是它们", "当前账号"),
):
    text = "\n".join(DownloadPage._intro_for(paid_plan(state), [vip]))
    check(f"会员={state} 的开场白", expect in text and forbid not in text, text)

check("全免费时不开场白", DownloadPage._intro_for(
    Plan(songs=[free], missing=[free]), [free]) == [], True)

# ====================================================================== #
# 另外两种歌词模式直接在已经下好的文件上验，不重新下载
print("[6] 歌词：另外两种模式", flush=True)
panel = window.console_tabs.widget(window.console_tabs.count() - 1)
pairs = [(song.base, song.id) for song in keep]
# 只取 keep 那两首 —— [3] 那一步也往同一个目录里下过东西，别把它的文件算进来
keep_bases = {song.base for song in keep}
audio = sorted(
    f
    for f in out.iterdir()
    if f.suffix in (".mp3", ".flac") and f.stat().st_size > 0 and f.stem in keep_bases
)


def runs() -> int:
    return panel.view.toPlainText().count("── 歌词：")


def embedded_lyric(path: Path) -> str:
    """像播放器那样把歌词读回来。

    刻意不走 :mod:`ncmgui.lyrics` —— 拿被测代码自己验自己，写错了也看不出来。
    """
    import mutagen.flac
    import mutagen.mp3

    if path.suffix == ".flac":
        return (mutagen.flac.FLAC(path).get("lyrics") or [""])[0]
    if path.suffix == ".mp3":
        frames = (mutagen.mp3.MP3(path).tags or {}).getall("USLT")
        return frames[0].text if frames else ""
    return ""


set_combo_data(page.lyric, "none")
set_combo_data(page.cover, "none")
check("歌词和封面都选「不下载」时压根不挂后处理", page._post_hook(keep) is None)

# 「写入音频标签」：先清掉上一轮生成的 .lrc，确保下面看的确实是标签
for stale in out.glob("*.lrc"):
    stale.unlink()
before = {p: (p.stat().st_size, mutagen.File(p).info.length) for p in audio}

page._start_lyrics(panel, pairs, str(out), "embed")
was = runs()
ok = pump(lambda: runs() > was, 240)
check("内嵌那一轮跑完了", ok, panel.view.toPlainText()[-200:])
print("      " + next(
    (ln for ln in panel.view.toPlainText().splitlines() if ln.startswith("── 歌词：") and runs() > was),
    "(没有汇总行)",
), flush=True)

check("内嵌模式没有产生 .lrc", not list(out.glob("*.lrc")), [p.name for p in out.glob("*.lrc")])
for path in audio:
    got = embedded_lyric(path)
    check(f"标签里读得到歌词：{path.name}", bool(got) and "[00:" in got, got[:60])
    size, length = before[path]
    check(f"音频数据本身没被动过：{path.name}",
          mutagen.File(path).info.length == length, (length, mutagen.File(path).info.length))
    if path.suffix == ".flac":
        # 标签写在元数据块里，音频流不该因此重编码 —— 时长必须一模一样
        check(f"flac 时长未变：{path.name}", mutagen.File(path).info.length > 0, length)

print(flush=True)
if fails:
    print("失败：", fails)
    sys.exit(1)
print("界面集成测试全部通过")
