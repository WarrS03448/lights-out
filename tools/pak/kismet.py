#!/usr/bin/env python3
"""Kismet (Blueprint VM) bytecode disassembler for cooked UE5 packages (unversioned, legacy .uasset/.uexp).
Usage: python3 kismet.py <pkg.uasset> <pkg.uexp> [function name substring ...]
Prints every UFunction export (or only the matching ones) as an indented expression tree with resolved
names, property references, object references (imports/exports) and string/name/number constants.
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pkgedit import CookedPackage

# ---------------------------------------------------------------- package helpers
class Pkg:
    def __init__(self, uasset, uexp):
        self.pk = CookedPackage.load(uasset, lenient=True)
        self.names = [n[0] for n in self.pk.names]
        self.b = open(uexp, "rb").read()
        self.hdr = self.pk.total_header_size
    def fname(self, b, p):
        idx, num = struct.unpack_from("<ii", b, p)
        s = self.names[idx] if 0 <= idx < len(self.names) else f"<badname {idx}>"
        return (s + (f"_{num-1}" if num else "")), p + 8
    def ref(self, idx):
        """Describe a package index."""
        if idx == 0: return "None"
        if idx < 0:
            im = self.pk.imports[-idx-1]
            outer = im[4]
            o = self.ref(outer) if outer else ""
            return f"{self.names[im[5]]}" + (f"@{o}" if o and not self.names[im[5]].startswith('/') else "")
        e = self.pk.exports[idx-1]
        return f"{self.names[e['name_idx']]}" + (f"_{e['name_num']-1}" if e['name_num'] else "")
    def ref_full(self, idx):
        if idx < 0:
            im = self.pk.imports[-idx-1]; return f"import {self.names[im[2]]} {self.names[im[5]]} (outer {self.ref(im[4])})"
        if idx > 0:
            e = self.pk.exports[idx-1]; return f"export[{idx-1}] {self.names[e['name_idx']]}"
        return "None"
    def export_data(self, i):
        e = self.pk.exports[i]; off = e["serial_offset"] - self.hdr
        return self.b[off:off + e["serial_size"]]
    def class_name(self, e):
        ci = e["class_idx"]
        if ci < 0: return self.names[self.pk.imports[-ci-1][5]]
        if ci > 0: return self.names[self.pk.exports[ci-1]["name_idx"]]
        return "None"

# ---------------------------------------------------------------- FField (property) parsing
def read_field(pkg, b, p):
    tname, p = pkg.fname(b, p); name, p = pkg.fname(b, p)
    p += 4  # EObjectFlags
    arraydim, elemsize = struct.unpack_from("<ii", b, p); p += 8
    pflags = struct.unpack_from("<Q", b, p)[0]; p += 8
    p += 2  # RepIndex
    _, p = pkg.fname(b, p)  # RepNotifyFunc
    p += 1  # BlueprintReplicationCondition
    f = {"type": tname, "name": name, "size": elemsize, "flags": pflags}
    if tname in ("ObjectProperty", "SoftObjectProperty", "WeakObjectProperty", "LazyObjectProperty", "ObjectPtrProperty"):
        f["class"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname in ("ClassProperty", "SoftClassProperty", "ClassPtrProperty"):
        f["class"] = struct.unpack_from("<i", b, p)[0]; p += 4
        f["meta"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "InterfaceProperty":
        f["class"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "StructProperty":
        f["struct"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "ByteProperty":
        f["enum"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "EnumProperty":
        f["enum"] = struct.unpack_from("<i", b, p)[0]; p += 4
        f["inner"], p = read_field(pkg, b, p)
    elif tname == "BoolProperty":
        p += 6
    elif tname in ("ArrayProperty", "SetProperty", "OptionalProperty"):
        f["inner"], p = read_field(pkg, b, p)
    elif tname == "MapProperty":
        f["key"], p = read_field(pkg, b, p); f["value"], p = read_field(pkg, b, p)
    elif tname in ("DelegateProperty", "MulticastInlineDelegateProperty", "MulticastSparseDelegateProperty", "MulticastDelegateProperty"):
        f["signature"] = struct.unpack_from("<i", b, p)[0]; p += 4
    elif tname == "FieldPathProperty":
        f["propclass"], p = pkg.fname(b, p)
    elif tname in ("TextProperty", "StrProperty", "NameProperty", "IntProperty", "FloatProperty", "DoubleProperty", "Int64Property",
                   "UInt32Property", "Int8Property", "Int16Property", "UInt16Property", "UInt64Property", "AnsiStrProperty", "Utf8StrProperty"):
        pass
    else:
        raise ValueError(f"unhandled field type {tname} ({name})")
    return f, p

def field_desc(pkg, f):
    s = f"{f['type']} {f['name']}"
    for k in ("class", "struct", "enum", "signature", "meta"):
        if k in f and f[k]: s += f" [{k}={pkg.ref(f[k])}]"
    if "inner" in f: s += f" <{field_desc(pkg, f['inner'])}>"
    if "key" in f: s += f" <{field_desc(pkg, f['key'])} -> {field_desc(pkg, f['value'])}>"
    return s

# ---------------------------------------------------------------- function export parsing
FUNC_Net = 0x40
def parse_function(pkg, i):
    d = pkg.export_data(i); p = 0
    hdr = struct.unpack_from("<H", d, p)[0]; p += 2
    assert hdr == 0x0100, f"unexpected unversioned header {hdr:#x}"
    p += 4  # UField::Next
    superstruct = struct.unpack_from("<i", d, p)[0]; p += 4
    nchildren = struct.unpack_from("<i", d, p)[0]; p += 4 + 4 * nchildren
    nprops = struct.unpack_from("<i", d, p)[0]; p += 4
    props = []
    for _ in range(nprops):
        f, p = read_field(pkg, d, p); props.append(f)
    bc_size, storage_size = struct.unpack_from("<ii", d, p); p += 8
    code = d[p:p + storage_size]; p += storage_size
    flags = struct.unpack_from("<I", d, p)[0]; p += 4
    if flags & FUNC_Net: p += 2
    eg_func, eg_off = struct.unpack_from("<ii", d, p); p += 8
    assert p == len(d), (p, len(d))
    return {"super": superstruct, "props": props, "code": code, "bc_size": bc_size, "flags": flags, "eg_func": eg_func, "eg_off": eg_off}

# ---------------------------------------------------------------- bytecode
class Dis:
    def __init__(self, pkg, code):
        self.pkg = pkg; self.c = code; self.p = 0; self.m = 0; self.out = []   # p = on-disk offset, m = in-memory offset (jump targets)
    # primitive readers
    def u8(self): v = self.c[self.p]; self.p += 1; self.m += 1; return v
    def u16(self): v = struct.unpack_from("<H", self.c, self.p)[0]; self.p += 2; self.m += 2; return v
    def i32(self): v = struct.unpack_from("<i", self.c, self.p)[0]; self.p += 4; self.m += 4; return v
    def u32(self): v = struct.unpack_from("<I", self.c, self.p)[0]; self.p += 4; self.m += 4; return v
    def i64(self): v = struct.unpack_from("<q", self.c, self.p)[0]; self.p += 8; self.m += 8; return v
    def u64(self): v = struct.unpack_from("<Q", self.c, self.p)[0]; self.p += 8; self.m += 8; return v
    def f32(self): v = struct.unpack_from("<f", self.c, self.p)[0]; self.p += 4; self.m += 4; return v
    def f64(self): v = struct.unpack_from("<d", self.c, self.p)[0]; self.p += 8; self.m += 8; return v
    def name(self): s, self.p = self.pkg.fname(self.c, self.p); self.m += 12; return s
    def obj(self): r = self.pkg.ref(self.i32()); self.m += 4; return r   # 4 on disk, 8 in memory
    def prop(self):
        m0 = self.m; n = self.i32(); path = [self.name() for _ in range(n)]; owner = self.i32(); self.m = m0 + 8   # FFieldPath on disk, 8-byte pointer in memory
        return ".".join(path) + (f"@{self.pkg.ref(owner)}" if owner else "")
    def astr(self):
        e = self.c.index(b"\0", self.p); s = self.c[self.p:e].decode("latin-1"); self.m += e + 1 - self.p; self.p = e + 1; return s
    def ustr(self):
        p = self.p
        while self.c[p:p+2] != b"\0\0": p += 2
        s = self.c[self.p:p].decode("utf-16-le"); self.m += p + 2 - self.p; self.p = p + 2; return s
    # emit
    def emit(self, depth, text): self.out.append("  " * depth + text)

    def expr(self, depth=0):
        """Parse one expression; emit lines; return a short inline summary string."""
        start = self.p; mstart = self.m; t = self.u8(); e = self.emit
        def one(text):
            e(depth, f"{mstart:04x}: {text}"); return text
        if t in (0x00, 0x01, 0x02, 0x48, 0x6C):
            kind = {0x00: "Local", 0x01: "Instance", 0x02: "Default", 0x48: "LocalOut", 0x6C: "SparseData"}[t]
            return one(f"${kind} {self.prop()}")
        if t == 0x33: return one(f"PropertyConst {self.prop()}")
        if t == 0x04: one("Return"); self.expr(depth + 1); return "Return"
        if t == 0x06: return one(f"Jump -> {self.u32():04x}")
        if t == 0x07:
            off = self.u32(); one(f"JumpIfNot -> {off:04x}"); self.expr(depth + 1); return "JumpIfNot"
        if t == 0x09:
            ln = self.u16(); dbg = self.u8(); one(f"Assert line={ln} debug={dbg}"); self.expr(depth + 1); return "Assert"
        if t == 0x0B: return one("Nothing")
        if t == 0x0C: return one(f"NothingInt32 {self.i32()}")
        if t == 0x0F:
            pr = self.prop(); one(f"Let ({pr})"); self.expr(depth + 1); self.expr(depth + 1); return "Let"
        if t == 0x11:
            pr = self.prop(); v = self.u8(); return one(f"BitFieldConst {pr} = {v}")
        if t in (0x12, 0x19, 0x1A):
            kind = {0x12: "ClassContext", 0x19: "Context", 0x1A: "Context_FailSilent"}[t]
            one(f"{kind}"); self.expr(depth + 1)
            skip = self.u32(); rv = self.prop(); e(depth + 1, f"  [skip={skip} rvalue={rv}]"); self.expr(depth + 1); return kind
        if t in (0x13, 0x2E):
            cls = self.obj(); one(f"{'MetaCast' if t == 0x13 else 'DynamicCast'} <{cls}>"); self.expr(depth + 1); return "Cast"
        if t in (0x14, 0x5F, 0x60, 0x44, 0x43):
            kind = {0x14: "LetBool", 0x5F: "LetObj", 0x60: "LetWeakObjPtr", 0x44: "LetDelegate", 0x43: "LetMulticastDelegate"}[t]
            one(kind); self.expr(depth + 1); self.expr(depth + 1); return kind
        if t == 0x15: return one("EndParmValue")
        if t == 0x16: return one("EndFunctionParms")
        if t == 0x17: return one("Self")
        if t == 0x18:
            skip = self.u32(); one(f"Skip {skip}"); self.expr(depth + 1); return "Skip"
        if t in (0x1B, 0x45):
            fn = self.name(); one(f"{'VirtualFunction' if t == 0x1B else 'LocalVirtualFunction'} {fn}(")
            while self.c[self.p] != 0x16: self.expr(depth + 1)
            self.p += 1; self.m += 1; e(depth, "  )"); return f"{fn}()"
        if t in (0x1C, 0x46, 0x68):
            fn = self.obj(); kind = {0x1C: "FinalFunction", 0x46: "LocalFinalFunction", 0x68: "CallMath"}[t]
            one(f"{kind} {fn}(")
            while self.c[self.p] != 0x16: self.expr(depth + 1)
            self.p += 1; self.m += 1; e(depth, "  )"); return f"{fn}()"
        if t == 0x63:
            sig = self.obj(); one(f"CallMulticastDelegate <{sig}>"); self.expr(depth + 1)
            while self.c[self.p] != 0x16: self.expr(depth + 1)
            self.p += 1; self.m += 1; e(depth, "  )"); return "CallMulticastDelegate"
        if t == 0x1D: return one(f"Int {self.i32()}")
        if t == 0x1E: return one(f"Float {self.f32()}")
        if t == 0x1F: return one(f"String {self.astr()!r}")
        if t == 0x34: return one(f"UString {self.ustr()!r}")
        if t == 0x20: return one(f"ObjectConst {self.obj()}")
        if t == 0x21: return one(f"NameConst {self.name()!r}")
        if t == 0x22: return one(f"RotationConst ({self.f64()}, {self.f64()}, {self.f64()})")
        if t == 0x23: return one(f"VectorConst ({self.f64()}, {self.f64()}, {self.f64()})")
        if t == 0x41: return one(f"Vector3fConst ({self.f32()}, {self.f32()}, {self.f32()})")
        if t == 0x2B: return one("TransformConst " + str([self.f64() for _ in range(10)]))
        if t in (0x24, 0x2C): return one(f"Byte {self.u8()}")
        if t == 0x25: return one("Int 0")
        if t == 0x26: return one("Int 1")
        if t == 0x27: return one("True")
        if t == 0x28: return one("False")
        if t == 0x29:
            kind = self.u8()
            if kind == 0: return one("TextConst <empty>")
            if kind == 1: one("TextConst localized"); self.expr(depth + 1); self.expr(depth + 1); self.expr(depth + 1); return "Text"
            if kind in (2, 3): one(f"TextConst {'invariant' if kind == 2 else 'literal'}"); self.expr(depth + 1); return "Text"
            if kind == 4:
                tbl = self.obj(); one(f"TextConst stringtable <{tbl}>"); self.expr(depth + 1); self.expr(depth + 1); return "Text"
            raise ValueError(f"text kind {kind}")
        if t == 0x2A: return one("NoObject")
        if t == 0x2D: return one("NoInterface")
        if t == 0x2F:
            st = self.obj(); sz = self.i32(); one(f"StructConst <{st}> size={sz} {{")
            while self.c[self.p] != 0x30: self.expr(depth + 1)
            self.p += 1; self.m += 1; e(depth, "  }"); return "StructConst"
        if t == 0x31:
            one("SetArray"); self.expr(depth + 1)
            while self.c[self.p] != 0x32: self.expr(depth + 1)
            self.p += 1; self.m += 1; return "SetArray"
        if t == 0x65:
            pr = self.prop(); n = self.i32(); one(f"ArrayConst <{pr}> n={n} [")
            while self.c[self.p] != 0x66: self.expr(depth + 1)
            self.p += 1; self.m += 1; e(depth, "  ]"); return "ArrayConst"
        if t == 0x39:
            one("SetSet"); self.expr(depth + 1); n = self.i32()
            while self.c[self.p] != 0x3A: self.expr(depth + 1)
            self.p += 1; self.m += 1; return "SetSet"
        if t == 0x3D:
            pr = self.prop(); n = self.i32(); one(f"SetConst <{pr}> n={n}")
            while self.c[self.p] != 0x3E: self.expr(depth + 1)
            self.p += 1; self.m += 1; return "SetConst"
        if t == 0x3B:
            one("SetMap"); self.expr(depth + 1); n = self.i32()
            while self.c[self.p] != 0x3C: self.expr(depth + 1)
            self.p += 1; self.m += 1; return "SetMap"
        if t == 0x3F:
            k = self.prop(); v = self.prop(); n = self.i32(); one(f"MapConst <{k} -> {v}> n={n}")
            while self.c[self.p] != 0x40: self.expr(depth + 1)
            self.p += 1; self.m += 1; return "MapConst"
        if t == 0x35: return one(f"Int64 {self.i64()}")
        if t == 0x36: return one(f"UInt64 {self.u64()}")
        if t == 0x37: return one(f"Double {self.f64()}")
        if t == 0x38:
            ct = self.u8(); one(f"Cast type={ct:#x}"); self.expr(depth + 1); return "Cast"
        if t == 0x42:
            pr = self.prop(); one(f"StructMemberContext {pr}"); self.expr(depth + 1); return "StructMember"
        if t == 0x4B: return one(f"InstanceDelegate {self.name()}")
        if t == 0x4C: return one(f"PushExecutionFlow -> {self.u32():04x}")
        if t == 0x4D: return one("PopExecutionFlow")
        if t == 0x4E: one("ComputedJump"); self.expr(depth + 1); return "ComputedJump"
        if t == 0x4F: one("PopExecutionFlowIfNot"); self.expr(depth + 1); return "PopIfNot"
        if t == 0x50: return one("Breakpoint")
        if t == 0x51: one("InterfaceContext"); self.expr(depth + 1); return "InterfaceContext"
        if t in (0x52, 0x54, 0x55):
            cls = self.obj(); one(f"InterfaceCast({t:#x}) <{cls}>"); self.expr(depth + 1); return "ICast"
        if t == 0x53: return one("EndOfScript")
        if t == 0x5A: return one("WireTracepoint")
        if t == 0x5E: return one("Tracepoint")
        if t == 0x5B: return one(f"SkipOffsetConst {self.u32():04x}")
        if t in (0x5C, 0x62):
            one("AddMulticastDelegate" if t == 0x5C else "RemoveMulticastDelegate"); self.expr(depth + 1); self.expr(depth + 1); return "Delegate"
        if t == 0x5D: one("ClearMulticastDelegate"); self.expr(depth + 1); return "Clear"
        if t == 0x61:
            fn = self.name(); one(f"BindDelegate {fn}"); self.expr(depth + 1); self.expr(depth + 1); return "BindDelegate"
        if t == 0x64:
            pr = self.prop(); one(f"LetValueOnPersistentFrame {pr}"); self.expr(depth + 1); return "LetPF"
        if t == 0x67: one("SoftObjectConst"); self.expr(depth + 1); return "SoftObjectConst"
        if t == 0x69:
            n = self.u16(); end = self.u32(); one(f"SwitchValue cases={n} end={end:04x}"); e(depth + 1, "index:"); self.expr(depth + 2)
            for i in range(n):
                e(depth + 1, f"case {i}:"); self.expr(depth + 2); nxt = self.u32(); e(depth + 2, f"[next={nxt:04x}] ->"); self.expr(depth + 2)
            e(depth + 1, "default:"); self.expr(depth + 2); return "Switch"
        if t == 0x6A:
            et = self.u8(); one(f"InstrumentationEvent {et}")
            if et == 6: self.name()
            return "Instr"
        if t == 0x6B: one("ArrayGetByRef"); self.expr(depth + 1); self.expr(depth + 1); return "ArrayGetByRef"
        if t == 0x6D: one("FieldPathConst"); self.expr(depth + 1); return "FieldPathConst"
        if t == 0x70:
            tid = self.i32(); jmp = self.u32(); one(f"AutoRtfmTransact id={tid} jump={jmp:04x}")
            while self.c[self.p] != 0x71: self.expr(depth + 1)
            return "Transact"
        if t == 0x71: tid = self.i32(); mode = self.u8(); return one(f"AutoRtfmStopTransact id={tid} mode={mode}")
        if t == 0x72: one("AutoRtfmAbortIfNot"); self.expr(depth + 1); return "AbortIfNot"
        raise ValueError(f"unknown token {t:#x} at {start:#x}")

    def run(self):
        while self.p < len(self.c):
            self.expr(0)
        return self.out

def list_functions(pkg):
    return [(i, pkg.names[e["name_idx"]]) for i, e in enumerate(pkg.pk.exports) if pkg.class_name(e) == "Function"]

if __name__ == "__main__":
    pkg = Pkg(sys.argv[1], sys.argv[2]); wanted = [w.lower() for w in sys.argv[3:]]
    for i, nm in list_functions(pkg):
        if wanted and not any(w in nm.lower() for w in wanted): continue
        fn = parse_function(pkg, i)
        print(f"\n===== function {nm} (export {i}) flags={fn['flags']:#x} super={pkg.ref(fn['super'])} bytecode={fn['bc_size']}/{len(fn['code'])} B eventgraph={pkg.ref(fn['eg_func'])}+{fn['eg_off']}")
        for f in fn["props"]:
            fl = f["flags"]; tag = ("param " if fl & 0x80 else "local ") + ("out " if fl & 0x100 else "") + ("ret " if fl & 0x400 else "")
            print(f"    {tag}{field_desc(pkg, f)}")
        d = Dis(pkg, fn["code"])
        try:
            lines = d.run()
        except Exception as ex:
            lines = d.out + [f"!! disassembly failed at {d.p:#x}: {ex}  next bytes: {fn['code'][d.p:d.p+24].hex(' ')}"]
        print("\n".join(lines))
