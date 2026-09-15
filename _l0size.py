import os

ROOT = r"C:/DATA/QClaw/Workspace/beacon-mfg"

L0_TARGETS = {
    "data/manifest.json": os.path.join(ROOT, "data", "manifest.json"),
    "data/gb": os.path.join(ROOT, "data", "gb"),
    "data/en": os.path.join(ROOT, "data", "en"),
    "skills/registry/fingerprint": os.path.join(ROOT, "skills", "registry", "fingerprint"),
}

def dir_size(path):
    total = 0
    count = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            fp = os.path.join(dp, fn)
            try:
                total += os.path.getsize(fp)
                count += 1
            except OSError:
                pass
    return total, count

def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

print("=== L0 体积统计 ===")
grand = 0
for name, p in L0_TARGETS.items():
    if os.path.isfile(p):
        s = os.path.getsize(p)
        print(f"{name:35s} file   {human(s):>12s}  ({s} bytes)")
        grand += s
    elif os.path.isdir(p):
        s, c = dir_size(p)
        print(f"{name:35s} dir    {human(s):>12s}  ({s} bytes, {c} files)")
        grand += s
    else:
        print(f"{name:35s} MISSING")
print(f"{'TOTAL L0':35s}        {human(grand):>12s}  ({grand} bytes)")

# dist/site existing size
site = os.path.join(ROOT, "dist", "site")
if os.path.isdir(site):
    s, c = dir_size(site)
    print(f"\ndist/site 现有体积: {human(s)} ({s} bytes, {c} files)")
else:
    print(f"\ndist/site 不存在: {site}")

# also data/index.json, phone-index.jsonl, region-index.json, gb-index.json
extra = ["data/index.json", "data/gb-index.json", "data/phone-index.jsonl", "data/region-index.json", "data/fetch_cursor.json"]
print("\n=== 其他 L0 索引文件 ===")
for e in extra:
    fp = os.path.join(ROOT, *e.split("/"))
    if os.path.isfile(fp):
        s = os.path.getsize(fp)
        print(f"{e:35s} {human(s):>12s}  ({s} bytes)")
        grand += s
print(f"{'GRAND TOTAL (含索引)':35s}        {human(grand):>12s}  ({grand} bytes)")
