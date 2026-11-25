#!/usr/bin/env python3
"""
出改动包的固定命令（离线快照 / 本机不可编译）。每轮都用它，不为某一轮另写打包或校验脚本。

用法：
  python pack.py --handoff handoff/<需求>-r<N> --repo <目录>=<基线commit> [--repo ...]
                 [--prev <上一轮包目录>] [--config <precheck.json>]
  python pack.py --selftest

做四件事，全部按字节读写，不经过终端重定向：
  1 全量    patches/<目录>.patch = 基线..HEAD。只含该目录，路径相对该目录：真实仓里用
             git apply --directory=<该目录在真实仓的相对路径> 打，目录就是仓根则不加
  2 相邻增量 <目录>-r<N-1>to-r<N>.patch = 「基线 + 上一轮全量」..HEAD。上一轮全量取自 --prev，
             缺省找同级 <需求>-r<N-1>，再找 _archived/<需求>/r<N-1>。上一轮全量打不到当前基线（基线换过）
             或与本轮无差异时不出增量
  3 自证    临时索引里「基线 + 全量」「上一轮终态 + 增量」的目录树都等于 HEAD，不等即 FAIL，不动工作区
  4 指纹    preimage.txt：增量触及的每个文件在打增量之前的 git blob（新增文件记 new），供真实仓打增量前核对
同级更早的 <需求>-r<K> 移入 _archived/<需求>/r<K>。README、manifest、precheck-report 不归本脚本管。
patch 文件名可用 config.patch_names（patch 名 -> 目录名）改名。末行 RESULT: PASS / FAIL，退出码 0 / 1。
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path


class PackError(Exception):
    pass


def git(root, *args, index=None, check=True):
    env = dict(os.environ, GIT_INDEX_FILE=str(index)) if index else None
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=env)
    if check and r.returncode != 0:
        raise PackError(f"git {' '.join(args[:3])} 失败：{r.stderr.decode('utf-8', 'ignore').strip()[:300]}")
    return r


def text(root, *args, **kw):
    return git(root, *args, **kw).stdout.decode("utf-8", "ignore").strip()


def tree_after(root, start, patch, tmp, prefix):
    """临时索引里从 start 起、在 prefix 目录下打上 patch，返回目录树 id；打不上返回 None。不碰工作区和真实索引。"""
    index = Path(tmp) / "index"
    if index.exists(): index.unlink()
    git(root, "read-tree", start, index=index)
    where = [f"--directory={prefix.rstrip('/')}"] if prefix else []
    if git(root, "apply", "--cached", "--whitespace=nowarn", *where, str(patch), index=index, check=False).returncode != 0:
        return None
    return text(root, "write-tree", index=index)


def subtree(root, tree, prefix):
    return text(root, "rev-parse", f"{tree}:{prefix.rstrip('/')}" if prefix else f"{tree}^{{tree}}")


def find_prev(handoff, name, n):
    for p in (handoff.parent / f"{name}-r{n - 1}", handoff.parent / "_archived" / name / f"r{n - 1}"):
        if (p / "patches").is_dir(): return p
    return None


def pack_repo(d, base, handoff, prev, n, alias, tmp, preimage):
    root = Path(text(d, "rev-parse", "--show-toplevel"))
    prefix = text(d, "rev-parse", "--show-prefix")
    diff = lambda *x: git(d, "diff", "--relative", "--no-abbrev", *x)  # 路径相对 d，只含 d 下的文件
    if text(d, "status", "--porcelain", "-uall", "--", "."):
        raise PackError(f"{d.name} 有未提交文件，先 commit 再出包")
    want = subtree(root, "HEAD", prefix)
    full = diff("--binary", "--full-index", base, "HEAD").stdout
    if not full.strip(): raise PackError(f"{d.name} 相对基线 {base} 没有改动（基线给错，或不该传这个 --repo）")
    fp = handoff / "patches" / f"{alias}.patch"
    fp.write_bytes(full)
    got = tree_after(root, base, fp, tmp, prefix)
    if got is None or subtree(root, got, prefix) != want:
        raise PackError(f"{alias}.patch 自证失败：基线 + 全量 不等于 HEAD")
    msg = f"{alias}: 全量 {full.count(b'diff --git ')} 文件 自证 PASS"
    if n == 1: return msg
    pp = prev / "patches" / f"{alias}.patch" if prev else None
    if pp is None or not pp.exists(): return msg + f"；增量 无（没有 r{n - 1} 的全量）"
    before = tree_after(root, base, pp, tmp, prefix)
    if before is None: return msg + f"；增量 无（r{n - 1} 全量打不到当前基线，基线换过，本轮只打全量）"
    inc = diff("--binary", "--full-index", before, "HEAD").stdout
    if not inc.strip(): return msg + f"；增量 无（与 r{n - 1} 无差异）"
    ip = handoff / f"{alias}-r{n - 1}to-r{n}.patch"
    ip.write_bytes(inc)
    got = tree_after(root, before, ip, tmp, prefix)
    if got is None or subtree(root, got, prefix) != want:
        raise PackError(f"{ip.name} 自证失败：r{n - 1} 终态 + 增量 不等于 HEAD")
    rows = diff("--raw", "-z", before, "HEAD").stdout.decode("utf-8", "ignore").split("\0")
    for meta, path in zip(rows[0::2], rows[1::2]):
        old = meta.split()[2]
        preimage.append(f"{ip.name}  {path}  {'new' if set(old) == {'0'} else old}")
    return msg + f"；增量 r{n - 1}→r{n} {len(rows) // 2} 文件 自证 PASS"


def pack(handoff, repos, prev=None, config=None):
    handoff = Path(handoff).resolve()
    m = re.match(r"(.+)-r(\d+)$", handoff.name)
    if not m: raise PackError(f"改动包目录名须是 <需求>-r<N>：{handoff.name}")
    name, n = m.group(1), int(m.group(2))
    names = {}
    if config:
        names = {v: k for k, v in json.loads(Path(config).read_text(encoding="utf-8")).get("patch_names", {}).items()}
    prev = Path(prev).resolve() if prev else find_prev(handoff, name, n)
    (handoff / "patches").mkdir(parents=True, exist_ok=True)
    for old in [*handoff.glob("patches/*.patch"), *handoff.glob("*-r*to-r*.patch"), handoff / "preimage.txt"]:
        if old.exists(): old.unlink()
    lines, preimage = [], []
    with tempfile.TemporaryDirectory(prefix="sop-pack-") as tmp:
        for spec in repos:
            path, _, base = spec.partition("=")
            d = Path(path).resolve()
            if not base: raise PackError(f"--repo {path} 缺基线：写成 <目录>=<基线commit>")
            lines.append(pack_repo(d, base, handoff, prev, n, names.get(d.name, d.name), tmp, preimage))
    if preimage: (handoff / "preimage.txt").write_bytes(("\n".join(preimage) + "\n").encode("utf-8"))
    for old in sorted(handoff.parent.glob(f"{name}-r*")):
        k = re.search(r"-r(\d+)$", old.name)
        if old.is_dir() and k and int(k.group(1)) < n:
            dest = handoff.parent / "_archived" / name / f"r{k.group(1)}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists(): shutil.rmtree(dest)
            shutil.move(str(old), str(dest)); lines.append(f"已归档 {old.name} → _archived/{name}/r{k.group(1)}")
    return lines


def selftest():
    T = Path(tempfile.mkdtemp(prefix="pack-selftest-"))
    fails = []
    try:
        mono = T / "mono"; svc = mono / "svc-a"; svc.mkdir(parents=True); (mono / "other").mkdir()
        g = lambda *x: subprocess.run(["git", "-C", str(mono), "-c", "user.email=a@b", "-c", "user.name=a", "-c", "core.autocrlf=false", *x], capture_output=True)
        commit = lambda msg: (g("add", "-A"), g("commit", "-qm", msg), text(mono, "rev-parse", "HEAD"))[2]
        g("init", "-q")
        (svc / "A.java").write_bytes(b"class A {}\r\n"); (svc / "logo.bin").write_bytes(bytes(range(256))); (mono / "other/x.txt").write_bytes(b"x\n")
        base = commit("base")
        (svc / "A.java").write_bytes(b"class A { int a; }\r\n"); (mono / "other/x.txt").write_bytes(b"y\n")
        commit("r1")
        ho = T / "handoff"
        out1 = pack(ho / "req-r1", [f"{svc}={base}"])
        if not (ho / "req-r1/patches/svc-a.patch").exists() or "全量 1 文件 自证 PASS" not in out1[0]: fails.append(f"r1 全量异常 {out1}")
        p1 = (ho / "req-r1/patches/svc-a.patch").read_bytes()
        if b"x.txt" in p1: fails.append("全量混进了目录外的文件")
        if b"a/A.java" not in p1 or b"svc-a/" in p1: fails.append("patch 路径应相对 --repo 目录")
        if list((ho / "req-r1").glob("*to-r*.patch")): fails.append("r1 不应有增量")
        if "全量 2 文件 自证 PASS" not in pack(T / "whole/all-r1", [f"{mono}={base}"])[0]: fails.append("--repo 是仓根时全量异常")
        (svc / "A.java").write_bytes(b"class A { int a; int b; }\r\n"); (svc / "B.java").write_bytes(b"class B {}\n"); (svc / "logo.bin").write_bytes(bytes(range(255, -1, -1)))
        commit("r2")
        out2 = pack(ho / "req-r2", [f"{svc}={base}"])
        inc, pre = ho / "req-r2/svc-a-r1to-r2.patch", ho / "req-r2/preimage.txt"
        if not inc.exists() or "增量 r1→r2 3 文件 自证 PASS" not in out2[0]: fails.append(f"r2 增量异常 {out2}")
        ptxt = pre.read_text(encoding="utf-8") if pre.exists() else ""
        blob = text(mono, "rev-parse", "HEAD~1:svc-a/A.java")
        if f"svc-a-r1to-r2.patch  A.java  {blob}" not in ptxt or "  B.java  new" not in ptxt: fails.append(f"preimage 异常 {ptxt!r}")
        if (ho / "req-r1").exists() or not (ho / "_archived/req/r1/patches/svc-a.patch").exists(): fails.append("r1 应移入 _archived")
        out2b = pack(ho / "req-r2", [f"{svc}={base}"])
        if "增量 r1→r2 3 文件" not in out2b[0]: fails.append("重跑应从 _archived 找到上一轮")
        if text(mono, "status", "--porcelain"): fails.append("出包不应改动工作区或索引")
        (svc / "A.java").write_bytes(b"dirty\n")
        for args, want in ((([f"{svc}={base}"],), "未提交"), (([str(svc)],), "缺基线")):
            try: pack(ho / "req-r3", *args); fails.append(f"应报「{want}」")
            except PackError as e:
                if want not in str(e): fails.append(f"应报「{want}」，实际 {e}")
        g("checkout", "-q", "--", "."); newbase = text(mono, "rev-parse", "HEAD~1")
        out3 = pack(ho / "req-r3", [f"{svc}={newbase}"])
        if "基线换过" not in out3[0]: fails.append(f"基线换过时不应出增量 {out3}")
    except PackError as e:
        fails.append(f"异常 {e}")
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print("\n".join(["SELFTEST FAIL", *(" - " + f for f in fails)]) if fails else "SELFTEST PASS（全量、相邻增量、自证、preimage、归档、重跑、未提交、基线换过）")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--handoff", help="本轮改动包目录 handoff/<需求>-r<N>")
    ap.add_argument("--repo", action="append", default=[], help="<目录>=<基线commit>；独立仓或大仓子目录")
    ap.add_argument("--prev", help="上一轮包目录；缺省自动找")
    ap.add_argument("--config", help="precheck.json（读 patch_names）")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.selftest: sys.exit(selftest())
    if not a.handoff or not a.repo: ap.error("--handoff 与至少一个 --repo 必填")
    try:
        print("\n".join(pack(a.handoff, a.repo, a.prev, a.config))); print("RESULT: PASS")
    except PackError as e:
        print(f"ERROR {e}"); print("RESULT: FAIL"); sys.exit(1)


if __name__ == "__main__":
    main()
