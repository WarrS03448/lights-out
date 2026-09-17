import sys, struct, os, collections; sys.path.insert(0,"/tmp")
from pkgedit import CookedPackage
from pakfile import PakSet
ps = PakSet("/mnt/user-data/uploads/Bodycam/Bodycam/Content/Paks")
os.makedirs("/tmp/extlv", exist_ok=True)
def get(full):
    base = os.path.basename(full)[:-5]
    for ext in (".umap", ".uexp"):
        p = f"/tmp/extlv/{base}{ext}"
        if not os.path.exists(p): open(p,"wb").write(ps.read(full[:-5]+ext))
    return f"/tmp/extlv/{base}.umap"
def objname(pk, names, idx):
    if idx < 0: return names[pk.imports[-idx-1][5]]
    if idx > 0: return names[pk.exports[idx-1]["name_idx"]]
    return "None"
def load(lvl):
    f = get("Bodycam/Content/"+lvl+".umap"); pk = CookedPackage.load(f, lenient=True); names=[n[0] for n in pk.names]
    return pk, names, open(f[:-5]+".uexp","rb").read()
if __name__ == "__main__":
    for lvl in sys.argv[1:]:
        pk, names, u = load(lvl)
        cnt = collections.Counter(objname(pk, names, e["class_idx"]) for e in pk.exports)
        print(lvl, len(pk.exports), "exports")
        for k,v in cnt.most_common():
            if any(s in k.lower() for s in ("spawn","start","team","zone","flag","objective","point","volume","site","hard","bomb","trigger","area")): print("   ", v, k)
