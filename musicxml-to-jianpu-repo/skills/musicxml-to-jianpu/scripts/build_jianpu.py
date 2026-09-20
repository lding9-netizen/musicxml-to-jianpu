#!/usr/bin/env python3
"""把 MusicXML 乐谱做成"打开即显示简谱"的独立网页。

用法:
    python3 build_jianpu.py 乐谱.mxl -o 输出目录或文件.html
    python3 build_jianpu.py 乐谱.mxl --parts "二胡,曲笛"   # 只预选这些声部（名称包含匹配，或声部 id）
    python3 build_jianpu.py 乐谱.mxl --summary-only          # 只打印乐谱摘要，不生成网页

转换本身由 assets/jianpu-template.html 里的页面脚本在浏览器中完成；
本脚本只做三件事：识别/校验输入、打印摘要、把乐谱原文件（base64）嵌进模板。
只用标准库，无需安装任何依赖。
"""
import argparse
import base64
import html
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "jianpu-template.html"
PLACEHOLDER = "<!--EMBEDDED_SCORE-->"
MAX_OUTPUT_MB = 15  # 已发布网页的上限是 16 MB


class InputError(Exception):
    """输入文件有问题——消息本身就是要转告用户的话。"""


# ---------------------------------------------------------------- 读取输入 --
def load_score(path: Path):
    """返回 (要嵌入的原始字节, 乐谱 XML 字节)。"""
    raw = path.read_bytes()
    if not raw:
        raise InputError("文件是空的。")
    if raw[:4] == b"%PDF":
        raise InputError(
            "这是 PDF 乐谱，不能直接转换：本工具读的是 MusicXML，而不是图片或 PDF。"
            "需要先用乐谱识别（OMR）软件（如 Audiveris）把它转成 MusicXML，识别结果通常还要人工校对；"
            "如果乐谱原本就是用 MuseScore / Sibelius / Finale 做的，直接从软件里「导出 MusicXML」更准确。")
    if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
        raise InputError("这是图片，不能直接转换。需要先用乐谱识别（OMR）软件转成 MusicXML；"
                         "如果有原始的打谱软件工程文件，直接导出 MusicXML 更准确。")
    if raw[:4] == b"MThd":
        raise InputError("这是 MIDI 文件。MIDI 没有完整的记谱信息（时值、连音线、调号等），转成简谱不可靠。"
                         "建议先用打谱软件导入 MIDI 整理好，再导出 MusicXML。")

    if raw[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise InputError("压缩包已损坏，无法打开。")
        names = zf.namelist()
        if any(n.lower().endswith((".mscx", ".mscz")) for n in names):
            raise InputError("这是 MuseScore 的原生格式（.mscz），不是 MusicXML。"
                             "请在 MuseScore 里选「文件 → 导出 → MusicXML」，保存为 .mxl 或 .musicxml。")
        xml_name = None
        if "META-INF/container.xml" in names:
            try:
                for rf in ET.fromstring(zf.read("META-INF/container.xml")).iter("rootfile"):
                    fp = rf.get("full-path")
                    if fp and fp in names:
                        xml_name = fp
                        break
            except ET.ParseError:
                pass
        if not xml_name:
            cands = [n for n in names
                     if n.lower().endswith((".xml", ".musicxml")) and not n.startswith("META-INF/")]
            xml_name = cands[0] if cands else None
        if not xml_name:
            raise InputError("压缩包里找不到乐谱文件，这可能不是 .mxl。")
        return raw, zf.read(xml_name)
    return raw, raw


def parse_root(xml_bytes: bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise InputError(f"不是有效的 XML，文件可能已损坏或不是 MusicXML（{e}）。")
    tag = root.tag.split("}")[-1]
    if tag == "score-timewise":
        raise InputError("这是 timewise 格式的 MusicXML，本工具只支持常规的 partwise 格式，"
                         "请在打谱软件中重新导出（多数软件默认就是 partwise）。")
    if tag != "score-partwise":
        raise InputError("这不是 MusicXML 乐谱文件（找不到 score-partwise）。")
    return root


# -------------------------------------------------------------------- 摘要 --
MAJOR_BY_FIFTHS = {
    -7: "♭C", -6: "♭G", -5: "♭D", -4: "♭A", -3: "♭E", -2: "♭B", -1: "F", 0: "C",
    1: "G", 2: "D", 3: "A", 4: "E", 5: "B", 6: "♯F", 7: "♯C",
}


def summarize(root):
    names, instr = {}, {}
    for sp in root.findall("part-list/score-part"):
        pid = sp.get("id")
        names[pid] = sp.findtext("part-name") or pid
        instr[pid] = {si.get("id"): (si.findtext("instrument-name") or "") for si in sp.findall("score-instrument")}

    parts, key_changes, time_changes = [], [], []
    first_key = first_time = None
    tempos, repeats, feats = [], 0, Counter()

    for idx, p in enumerate(root.findall("part")):
        pid = p.get("id")
        info = {"id": pid, "name": names.get(pid, pid), "measures": 0, "pitched": 0,
                "unpitched": 0, "sounds": Counter()}
        for m in p.findall("measure"):
            info["measures"] += 1
            a = m.find("attributes")
            if a is not None:
                k, t = a.find("key"), a.find("time")
                if k is not None:
                    key = (int(float(k.findtext("fifths") or 0)), k.findtext("mode") or "major")
                    if first_key is None:
                        first_key = key
                    elif idx == 0 and key != first_key:
                        key_changes.append(m.get("number"))
                if t is not None:
                    tm = (t.findtext("beats"), t.findtext("beat-type"))
                    if first_time is None:
                        first_time = tm
                    elif idx == 0 and tm != first_time:
                        time_changes.append(m.get("number"))
            if idx == 0:
                for s in m.iter("sound"):
                    if s.get("tempo"):
                        tempos.append(round(float(s.get("tempo"))))
                repeats += len(m.findall("barline/repeat")) // 2
            for n in m.findall("note"):
                if n.find("rest") is not None:
                    continue
                if n.find("unpitched") is not None:
                    info["unpitched"] += 1
                    ie = n.find("instrument")
                    iid = ie.get("id") if ie is not None else ""
                    info["sounds"][instr.get(pid, {}).get(iid) or iid or "?"] += 1
                elif n.find("pitch") is not None:
                    info["pitched"] += 1
                if n.find("grace") is not None:
                    feats["倚音"] += 1
                if n.find("time-modification") is not None:
                    feats["连音符（三连音等）"] += 1
                if n.find("lyric") is not None:
                    feats["歌词"] += 1
        parts.append(info)

    meta = {
        "title": root.findtext("work/work-title") or root.findtext("movement-title") or "",
        "composer": next((c.text for c in root.findall("identification/creator")
                          if c.get("type") == "composer" and c.text), ""),
    }
    return meta, parts, first_key, first_time, key_changes, time_changes, sorted(set(tempos)), repeats, feats


def print_summary(root):
    meta, parts, fk, ft, kc, tc, tempos, repeats, feats = summarize(root)
    print("── 乐谱摘要 ──")
    print(f"曲名：{meta['title'] or '（无）'}" + (f"　作曲：{meta['composer']}" if meta["composer"] else ""))
    if fk:
        fifths, mode = fk
        minor = "；标记为小调，按关系大调记谱" if mode == "minor" else ""
        if fifths:
            print(f"调号：{abs(fifths)} 个{'升' if fifths > 0 else '降'}号（1={MAJOR_BY_FIFTHS.get(fifths, '?')}{minor}）")
        else:
            print(f"调号：无升降号（1=C{minor}）")
    if ft:
        print(f"拍号：{ft[0]}/{ft[1]}")
    print(f"声部：{len(parts)} 个，{max((p['measures'] for p in parts), default=0)} 小节")
    for p in parts:
        if p["unpitched"] and not p["pitched"]:
            snd = "、".join(f"{k}×{v}" for k, v in p["sounds"].most_common())
            desc = f"打击乐（无音高），{p['unpitched']} 个音，音色：{snd}"
        elif p["pitched"] == 0:
            desc = "整首休止"
        else:
            desc = f"{p['pitched']} 个音"
        print(f"  - {p['name']}  [id={p['id']}]  {desc}")
    if tempos:
        print("速度标记：" + "、".join(f"♩={t}" for t in tempos))
    if repeats:
        print(f"反复记号：约 {repeats} 处")
    if feats:
        print("含有：" + "、".join(f"{k}（{v}）" for k, v in feats.items()))

    notes = []
    if kc:
        notes.append("第 " + "、".join(kc[:8]) + " 小节有变调（页面会在该处标出新的 1=）。")
    if tc:
        notes.append("第 " + "、".join(tc[:8]) + " 小节有拍号变化（页面会在该处标出）。")
    if any(p["unpitched"] and not p["pitched"] for p in parts):
        notes.append("含打击乐：无音高音符记作 ×；鼓类用 ⊗(圈×)=鼓心、×=鼓边，木鱼用 × 上/下加点表示高/低音，"
                     "页面里的「打击乐记号」一行可以手动调整。")
    silent = [p["name"] for p in parts if p["pitched"] == 0 and p["unpitched"] == 0]
    if silent:
        notes.append("这些声部整首都是休止（页面里会显示为一行 0 0）：" + "、".join(silent) + "。")
    if notes:
        print("提醒：")
        for n in notes:
            print("  · " + n)
    return meta, parts


# -------------------------------------------------------------------- 构建 --
def match_parts(wanted, parts):
    """与页面里的规则一致：声部 id 相等，或名称包含（不区分大小写）。"""
    hit = []
    for p in parts:
        if any(w.lower() == p["id"].lower() or w.lower() in p["name"].lower() for w in wanted):
            hit.append(p["name"])
    return hit


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("._") or "score"


def main():
    ap = argparse.ArgumentParser(description="MusicXML → 简谱网页")
    ap.add_argument("input", help=".mxl / .musicxml / .xml")
    ap.add_argument("-o", "--output", help="输出文件或目录（默认：当前目录，文件名为 <原文件名>_简谱.html）")
    ap.add_argument("--parts", help="预选的声部，逗号分隔（名称包含匹配，或声部 id）")
    ap.add_argument("--summary-only", action="store_true", help="只打印摘要")
    args = ap.parse_args()

    src = Path(args.input)
    try:
        if not src.is_file():
            raise InputError(f"找不到文件：{src}")
        raw, xml_bytes = load_score(src)
        root = parse_root(xml_bytes)
    except InputError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    meta, parts = print_summary(root)
    if args.summary_only:
        return 0

    wanted = [w.strip() for w in (args.parts or "").split(",") if w.strip()]
    if wanted:
        hit = match_parts(wanted, parts)
        if not hit:
            print("错误：没有声部匹配 --parts 给出的名称。可选声部：" + "、".join(p["name"] for p in parts),
                  file=sys.stderr)
            return 1
        print("预选声部：" + "、".join(hit))

    if not TEMPLATE.is_file():
        print(f"错误：找不到模板 {TEMPLATE}", file=sys.stderr)
        return 1
    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        print("错误：模板里的占位符 <!--EMBEDDED_SCORE--> 不是恰好一个。", file=sys.stderr)
        return 1

    b64 = base64.b64encode(raw).decode("ascii")
    b64 = "\n".join(b64[i:i + 160] for i in range(0, len(b64), 160))
    tag = ('<script type="application/octet-stream" id="embedded-score" data-name="%s" data-parts="%s">\n%s\n</script>'
           % (html.escape(src.name, quote=True), html.escape(",".join(wanted), quote=True), b64))
    out_html = template.replace(PLACEHOLDER, tag)

    default_name = safe_name(src.stem) + "_简谱.html"
    out = Path(args.output) if args.output else Path.cwd()
    if out.is_dir() or (not out.suffix and not out.exists()):
        out.mkdir(parents=True, exist_ok=True)
        out = out / default_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(out_html, encoding="utf-8")

    mb = out.stat().st_size / 1024 / 1024
    print(f"\n已生成：{out}（{mb:.2f} MB）")
    if mb > MAX_OUTPUT_MB:
        print(f"注意：文件超过 {MAX_OUTPUT_MB} MB，发布成网页链接可能失败；直接交付文件即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
