"""List a cooked BlueprintGeneratedClass: super, properties (in order), functions, SCS component nodes, interfaces."""
import sys, struct; sys.path.insert(0, "/tmp")
from kismet import Pkg, read_field, field_desc
def class_info(ua, ue):
    pkg = Pkg(ua, ue)
    for i, e in enumerate(pkg.pk.exports):
        if pkg.class_name(e) != "BlueprintGeneratedClass": continue
        d = pkg.export_data(i); p = 0
        nvals = 0
        while True:
            hdr = struct.unpack_from("<H", d, p)[0]; p += 2; nvals += hdr >> 9
            assert not (hdr & 0x80), "zero mask not handled"
            if hdr & 0x100: break
        p += 4 * nvals   # BlueprintGeneratedClass UPROPERTY values (object refs: SCS, ICH, ...)
        p += 4  # UField::Next
        superstruct = struct.unpack_from("<i", d, p)[0]; p += 4
        nchildren = struct.unpack_from("<i", d, p)[0]; children = list(struct.unpack_from(f"<{nchildren}i", d, p + 4)); p += 4 + 4 * nchildren
        nprops = struct.unpack_from("<i", d, p)[0]; p += 4
        props = []
        for _ in range(nprops):
            f, p = read_field(pkg, d, p); props.append(f)
        bc_size, storage_size = struct.unpack_from("<ii", d, p); p += 8 + storage_size
        # UClass part follows: FuncMap etc. -- skip parsing; grab interfaces by scanning exports instead
        print("CLASS", pkg.names[e["name_idx"]], "super:", pkg.ref_full(superstruct))
        print(" properties:", len(props))
        for k, f in enumerate(props): print(f"   #{k} {field_desc(pkg, f)}  flags={f['flags']:#x}")
        print(" children (functions):", [pkg.ref(c) for c in children])
    # SCS nodes
    for i, e in enumerate(pkg.pk.exports):
        cn = pkg.class_name(e)
        if cn == "SCS_Node":
            d = pkg.export_data(i)
            # unversioned: fragments then values; find ComponentClass (import) and InternalVariableName (FName)
            print(" SCS_Node", pkg.names[e["name_idx"]], e["name_num"], "bytes:", d.hex(" "))
        if cn in ("SimpleConstructionScript", "InheritableComponentHandler"):
            print(" ", cn, pkg.export_data(i).hex(" "))
    return pkg
def bgc_props(ua, ue):
    """Own properties (in cooked order) of the BlueprintGeneratedClass in a cooked package, as dicts with type/name/struct_name."""
    pkg = Pkg(ua, ue)
    for i, e in enumerate(pkg.pk.exports):
        if pkg.class_name(e) != "BlueprintGeneratedClass": continue
        d = pkg.export_data(i); p = 0; nvals = 0
        while True:
            hdr = struct.unpack_from("<H", d, p)[0]; p += 2; nvals += hdr >> 9
            assert not (hdr & 0x80), "zero mask not handled"
            if hdr & 0x100: break
        p += 4 * nvals + 4
        p += 4  # super
        nchildren = struct.unpack_from("<i", d, p)[0]; p += 4 + 4 * nchildren
        nprops = struct.unpack_from("<i", d, p)[0]; p += 4
        out = []
        for _ in range(nprops):
            f, p = read_field(pkg, d, p)
            if "struct" in f: f["struct_name"] = pkg.ref(f["struct"]).split("@")[0]
            out.append(f)
        return out
    raise ValueError("no BlueprintGeneratedClass export")

if __name__ == "__main__":
    class_info(sys.argv[1], sys.argv[2])
