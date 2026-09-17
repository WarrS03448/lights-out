#!/usr/bin/env python3
"""Parse a cooked UserDefinedStruct export (unversioned) to get its property schema, and decode DataTable rows.
Usage: python3 udstruct.py <Struct.uasset> <Struct.uexp> [<DT.uasset> <DT.uexp>]
"""
import os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from uasset import Package

def fname(b, p, pk):
    idx, num = struct.unpack_from("<ii", b, p)
    return pk.name(idx, num), p + 8

def fstr(b, p):
    n = struct.unpack_from("<i", b, p)[0]; p += 4
    if n < 0: s = b[p:p + (-n)*2].decode("utf-16-le").rstrip("\x00"); p += (-n)*2
    elif n == 0: s = ""
    else: s = b[p:p+n].decode("utf-8","replace").rstrip("\x00"); p += n
    return s, p

def read_field(b, p, pk):
    """Serialize a single FField (type name + FField + FProperty + subtype extras). Returns (dict, p)."""
    tname, p = fname(b, p, pk)
    name, p = fname(b, p, pk)
    flags = struct.unpack_from("<I", b, p)[0]; p += 4
    arraydim, elemsize = struct.unpack_from("<ii", b, p); p += 8
    pflags = struct.unpack_from("<Q", b, p)[0]; p += 8
    repindex = struct.unpack_from("<H", b, p)[0]; p += 2
    repnotify, p = fname(b, p, pk)
    repcond = b[p]; p += 1
    f = {"type": tname, "name": name, "size": elemsize, "pflags": pflags}
    if tname in ("ObjectProperty", "SoftObjectProperty", "ClassProperty", "SoftClassProperty", "WeakObjectProperty"):
        f["class"] = struct.unpack_from("<i", b, p)[0]; p += 4
        if tname in ("ClassProperty", "SoftClassProperty"):
            f["metaclass"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "StructProperty":
        f["struct"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "ByteProperty":
        f["enum"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "EnumProperty":
        f["enum"] = struct.unpack_from("<i", b, p)[0]; p += 4
        f["inner"], p = read_field(b, p, pk)
    elif tname == "BoolProperty":
        f["bool"] = b[p:p+6].hex(); p += 6  # FieldSize, ByteOffset, ByteMask, FieldMask, BoolSize, NativeBool
    elif tname in ("ArrayProperty", "SetProperty"):
        f["inner"], p = read_field(b, p, pk)
    elif tname == "MapProperty":
        f["key"], p = read_field(b, p, pk)
        f["value"], p = read_field(b, p, pk)
    elif tname in ("TextProperty", "StrProperty", "NameProperty", "IntProperty", "FloatProperty", "DoubleProperty",
                   "Int64Property", "UInt32Property", "Int8Property", "Int16Property", "UInt16Property", "UInt64Property"):
        pass
    else:
        raise ValueError(f"unhandled field type {tname}")
    return f, p

def parse_struct(uasset, uexp):
    pk = Package(uasset); b = open(uexp, "rb").read()
    imports = pk_imports(pk)
    p = 0
    hdr = struct.unpack_from("<H", b, p)[0]; p += 2  # UserDefinedStruct props header
    assert hdr == 0x0301, hex(hdr)
    guid = b[p:p+16].hex(); p += 16
    p += 4  # bHasGuid / trailer
    superstruct = struct.unpack_from("<i", b, p)[0]; p += 4
    nchildren = struct.unpack_from("<i", b, p)[0]; p += 4 + 4*nchildren
    nprops = struct.unpack_from("<i", b, p)[0]; p += 4
    props = []
    for _ in range(nprops):
        f, p = read_field(b, p, pk); props.append(f)
    return pk, imports, props, p, b

def pk_imports(pk):
    """Parse the import table: (ClassPackage FName, ClassName FName, OuterIndex int32, ObjectName FName, bImportOptional int32)."""
    b = pk.b; ints = pk.summary_tail_ints
    import_count, import_offset = ints[6], ints[7]
    out = []; p = import_offset
    for _ in range(import_count):
        cp, p = fname(b, p, pk); cn, p = fname(b, p, pk)
        outer = struct.unpack_from("<i", b, p)[0]; p += 4
        on, p = fname(b, p, pk)
        p += 4  # bImportOptional
        out.append((cp, cn, outer, on))
    return out

def describe_ref(idx, imports):
    if idx == 0: return "null"
    if idx < 0:
        cp, cn, outer, on = imports[-idx-1]
        return f"import[{-idx-1}] {cn} {on}"
    return f"export[{idx-1}]"

if __name__ == "__main__":
    pk, imports, props, endp, b = parse_struct(sys.argv[1], sys.argv[2])
    print("imports:")
    for i, im in enumerate(imports): print(f"  [{i}] class={im[1]} name={im[3]} outer={im[2]}")
    print("properties (serialization order):")
    def show(f, indent="  "):
        extra = ""
        for k in ("class", "struct", "enum"):
            if k in f: extra += f" {k}={describe_ref(f[k], imports)}"
        print(f"{indent}{f['type']:20} {f['name']}  size={f['size']}{extra}")
        if "inner" in f: show(f["inner"], indent + "    inner: ")
    for f in props: show(f)
    print("bytes after property list:", b[endp:endp+40].hex(" "), "... total", len(b))
