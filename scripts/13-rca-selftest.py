#!/usr/bin/env python3
"""13-rca-selftest.py

Negative fixtures for the RCA regression gates in 08-gate3.py.

Every fixture is generated in a temporary directory. No client artwork, no
signed-off job and no private Figma identifier is read. The test needs the
configured delivery ICC and a CMYK ICC from the local Ghostscript install.
"""

import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

spec = importlib.util.spec_from_file_location("gate3", os.path.join(HERE, "08-gate3.py"))
gate3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate3)

from pypdf import PdfReader, PdfWriter  # noqa: E402
from pypdf.generic import (ArrayObject, DecodedStreamObject,  # noqa: E402
                           DictionaryObject, FloatObject, NameObject,
                           NumberObject, TextStringObject)

SPEC = json.load(open(os.path.join(ROOT, "config", "print-spec.json"), encoding="utf-8"))
TARGET = SPEC["print"]["iccProfile"]
if not os.path.isabs(TARGET):
    TARGET = os.path.join(ROOT, TARGET)
TARGET_SHA = gate3._sha_bytes(open(TARGET, "rb").read())
GS = shutil.which("gs") or "/opt/homebrew/bin/gs"
MUTOOL = shutil.which("mutool")


def find_swop():
    roots = ["/opt/homebrew", "/usr/local", "/usr/share"]
    for root in roots:
        hits = glob.glob(os.path.join(root, "**", "default_cmyk.icc"), recursive=True)
        if hits:
            return sorted(hits)[-1]
    return None


SWOP = find_swop()


def make_base(path, icc_bytes, decl=True, output_intent=True):
    ps = os.path.join(os.path.dirname(path), "base.ps")
    with open(ps, "w", encoding="ascii") as fh:
        fh.write("<< /PageSize [595 842] >> setpagedevice\n"
                 "0.4 0.106 0.451 0.051 setcmykcolor 0 0 595 842 rectfill\n"
                 "showpage\n")
    subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                    "-sDEVICE=pdfwrite", "-o", path, ps], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    writer = PdfWriter(clone_from=PdfReader(path))
    page = writer.pages[0]
    res = page["/Resources"].get_object()
    cs = res.get("/ColorSpace")
    cs = cs.get_object() if cs is not None else DictionaryObject()
    res[NameObject("/ColorSpace")] = cs
    tint = DictionaryObject()
    tint[NameObject("/FunctionType")] = NumberObject(2)
    tint[NameObject("/Domain")] = ArrayObject([NumberObject(0), NumberObject(1)])
    tint[NameObject("/C0")] = ArrayObject([NumberObject(0)] * 4)
    tint[NameObject("/C1")] = ArrayObject([NumberObject(1)] * 4)
    tint[NameObject("/N")] = NumberObject(1)
    cs[NameObject("/CSAll")] = ArrayObject([
        NameObject("/Separation"), NameObject("/All"), NameObject("/DeviceCMYK"), tint])
    stream = DecodedStreamObject()
    stream.set_data(icc_bytes)
    stream[NameObject("/N")] = NumberObject(4)
    iref = writer._add_object(stream)
    if decl:
        cs[NameObject("/DefaultCMYK")] = ArrayObject([NameObject("/ICCBased"), iref])
    if output_intent:
        intent = DictionaryObject()
        intent[NameObject("/Type")] = NameObject("/OutputIntent")
        intent[NameObject("/S")] = NameObject("/GTS_PDFX")
        intent[NameObject("/OutputConditionIdentifier")] = TextStringObject("selftest")
        intent[NameObject("/RegistryName")] = TextStringObject("http://www.color.org")
        intent[NameObject("/DestOutputProfile")] = iref
        writer._root_object[NameObject("/OutputIntents")] = ArrayObject([intent])
    box = ArrayObject([FloatObject(0), FloatObject(0), FloatObject(595), FloatObject(842)])
    page[NameObject("/MediaBox")] = box
    page[NameObject("/TrimBox")] = box
    page[NameObject("/BleedBox")] = box
    writer.write(path)


def page_cs(page):
    res = page["/Resources"].get_object()
    cs = res.get("/ColorSpace")
    return res, (cs.get_object() if cs is not None else DictionaryObject())


def add_form(writer, page, resources, content, name="/FmTest"):
    form = DecodedStreamObject()
    form.set_data(content)
    form[NameObject("/Type")] = NameObject("/XObject")
    form[NameObject("/Subtype")] = NameObject("/Form")
    form[NameObject("/BBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(100), FloatObject(100)])
    if resources is not None:
        form[NameObject("/Resources")] = resources
    ref = writer._add_object(form)
    res, _ = page_cs(page)
    xo = res.get("/XObject")
    xo = xo.get_object() if xo is not None else DictionaryObject()
    xo[NameObject(name)] = ref
    res[NameObject("/XObject")] = xo
    contents = page["/Contents"]
    stream = contents.get_object()
    stream.set_data(stream.get_data() + b"\nq %s Do Q\n" % name.encode("ascii"))
    return ref


def main():
    checks = {}

    def expect(name, actual, expected, evidence=None):
        checks[name] = {"pass": actual == expected, "expected": expected,
                        "actual": actual, "evidence": evidence}

    if not os.path.exists(TARGET):
        print("delivery ICC missing: %s" % TARGET, file=sys.stderr)
        return 66

    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "base.pdf")
        make_base(base, open(TARGET, "rb").read())

        def status(path, fn):
            return fn(path)

        expect("defaultcmyk.base", "pass" if gate3.defaultcmyk_check(base, TARGET_SHA).get("ok") else "fail", "pass")

        no_dcm = os.path.join(tmp, "no-defaultcmyk.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        _, cs = page_cs(writer.pages[0])
        del cs["/DefaultCMYK"]
        writer.write(no_dcm)
        expect("defaultcmyk.missing", "pass" if gate3.defaultcmyk_check(no_dcm, TARGET_SHA).get("ok") else "fail", "fail")

        bare = os.path.join(tmp, "bare-defaultcmyk.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        _, cs = page_cs(writer.pages[0])
        dcm = cs["/DefaultCMYK"].get_object()
        cs[NameObject("/DefaultCMYK")] = dcm[1]
        writer.write(bare)
        expect("defaultcmyk.bareStream", "pass" if gate3.defaultcmyk_check(bare, TARGET_SHA).get("ok") else "fail", "fail")

        if SWOP:
            wrong = os.path.join(tmp, "wrong-defaultcmyk.pdf")
            make_base(wrong, open(SWOP, "rb").read())
            expect("defaultcmyk.wrongICC", "pass" if gate3.defaultcmyk_check(wrong, TARGET_SHA).get("ok") else "fail", "fail")

        no_oi = os.path.join(tmp, "no-outputintent.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        del writer._root_object["/OutputIntents"]
        writer.write(no_oi)
        expect("outputintent.removed", len(gate3.pdfprobe.output_intents(no_oi)), 0)

        wrong_subtype = os.path.join(tmp, "wrong-subtype.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        oi_dict = writer._root_object["/OutputIntents"][0].get_object()
        oi_dict[NameObject("/S")] = NameObject("/GTS_PDFA1")
        writer.write(wrong_subtype)
        subtype = (gate3.pdfprobe.output_intents(wrong_subtype)[0] or {}).get("subtype")
        expect("outputintent.subtype", "fail" if subtype != "/GTS_PDFX" else "pass", "fail",
               {"subtype": subtype})

        conv = {"targetICC": TARGET, "targetICCsha256": TARGET_SHA}
        oi = (gate3.pdfprobe.output_intents(base)[0].get("destProfile") or {}).get("sha256")
        dcm_sha = gate3.defaultcmyk_check(base, TARGET_SHA).get("streamSha256")
        expect("profilechain.base", gate3.profilechain_check(conv, TARGET_SHA, oi, dcm_sha)["status"], "pass")
        expect("profilechain.missingOutputIntent",
               gate3.profilechain_check(conv, TARGET_SHA, None, dcm_sha)["status"], "fail")
        expect("profilechain.missingConversionRecord",
               gate3.profilechain_check(None, TARGET_SHA, oi, dcm_sha)["status"], "pending")

        undefined = os.path.join(tmp, "undefined-cs.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        resources = DictionaryObject()
        resources[NameObject("/ColorSpace")] = DictionaryObject({
            NameObject("/CS0"): NameObject("/DeviceCMYK")})
        add_form(writer, writer.pages[0], resources,
                 b"/CSX cs 0.4 0.106 0.451 0.051 scn 0 0 100 100 re f")
        writer.write(undefined)
        undef = gate3.inheritedcs_check(undefined, TARGET_SHA)
        expect("inheritedcs.undefinedName", undef["status"], "fail",
               {"unresolved": undef["unresolved"]})

        srgb_path = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
        if os.path.exists(srgb_path):
            rgb_form = os.path.join(tmp, "rgb-form.pdf")
            writer = PdfWriter(clone_from=PdfReader(base))
            rgb_stream = DecodedStreamObject()
            rgb_stream.set_data(open(srgb_path, "rb").read())
            rgb_stream[NameObject("/N")] = NumberObject(3)
            resources = DictionaryObject()
            resources[NameObject("/ColorSpace")] = DictionaryObject({
                NameObject("/CS0"): ArrayObject(
                    [NameObject("/ICCBased"), writer._add_object(rgb_stream)])})
            add_form(writer, writer.pages[0], resources,
                     b"/CS0 cs 0.2 0.4 0.3 scn 0 0 100 100 re f")
            writer.write(rgb_form)
            rgbf = gate3.inheritedcs_check(rgb_form, TARGET_SHA)
            expect("inheritedcs.rgbICC", rgbf["status"], "fail",
                   {"rgbSpaces": rgbf["rgbSpaces"]})

        inherited = os.path.join(tmp, "inherited-form.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        add_form(writer, writer.pages[0], None, b"/CSAll cs 1 scn 0 0 100 100 re f")
        writer.write(inherited)
        inh = gate3.inheritedcs_check(inherited, TARGET_SHA)
        expect("inheritedcs.formWithoutResources", inh["status"], "pass",
               {"inheritedForms": inh["inheritedForms"]})

        rgb_only = os.path.join(tmp, "rgb-only.pdf")
        writer = PdfWriter(clone_from=PdfReader(base))
        stream = DecodedStreamObject()
        stream.set_data(b"q 1 0 0 rg 0 0 200 200 re f Q\n")
        writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
        writer.write(rgb_only)
        expect("inkStored.noCMYK", gate3.stored_ink_check(rgb_only)["status"], "pending")

        base_stored = gate3.stored_ink_check(base)
        managed = gate3.managed_separations_check(
            base, base_stored, gate3.defaultcmyk_check(base, TARGET_SHA), dpi=72)
        expect("separationsManaged.controlProbe", managed["status"], "pass",
               {"mechanism": managed.get("mechanism"), "plateInk": managed.get("plateInk"),
                "probeInk": managed.get("probeInk")})

        if SWOP:
            wrong_stored = gate3.stored_ink_check(wrong)
            wrong_managed = gate3.managed_separations_check(
                wrong, wrong_stored, gate3.defaultcmyk_check(wrong, TARGET_SHA), dpi=72)
            expect("separationsManaged.wrongDeclaration", wrong_managed["status"], "fail",
                   {"mechanism": wrong_managed.get("mechanism")})

        green = (40.0, 10.6, 45.1, 5.1)
        plates = [(18.0, 84.3, 33.3, 33.7), green]
        expect("classifier.managedConfirmed",
               gate3._classify_managed(plates, [38.04, 13.73, 47.06, 0.0],
                                       [40.0, 10.59, 45.1, 5.1], True, True)["status"], "pass")
        expect("classifier.managedUnexplained",
               gate3._classify_managed(plates, [38.04, 13.73, 47.06, 0.0],
                                       None, False, False)["status"], "fail")
        expect("classifier.managedWrongDeclaration",
               gate3._classify_managed(plates, [40.0, 10.6, 45.1, 5.1],
                                       None, False, True)["status"], "fail")
        expect("classifier.renderersAgree",
               gate3._classify_renderers({"median": 0.3, "p99": 1.2,
                                          "over5Percent": 0.0})["status"], "pass")
        expect("classifier.renderersDisagree",
               gate3._classify_renderers({"median": 9.0, "p99": 30.0,
                                          "over5Percent": 12.0})["status"], "fail")

        boxes = {"TrimBox": [0, 0, 1190.55, 841.89]}
        valid_fold = {"fold": {"type": "half", "axis": "vertical",
                               "positionMm": 210, "panels": [210, 210]}}
        expect("fold.halfAtCentre", gate3.fold_check(valid_fold, SPEC, boxes)["status"], "pass")
        expect("fold.offCentre",
               gate3.fold_check({"fold": {"type": "half", "axis": "vertical",
                                          "positionMm": 200, "panels": [210, 210]}},
                                SPEC, boxes)["status"], "fail")
        expect("fold.unequalPanels",
               gate3.fold_check({"fold": {"type": "half", "axis": "vertical",
                                          "positionMm": 210, "panels": [210, 200]}},
                                SPEC, boxes)["status"], "fail")

        def geometry_case(kind, page_mm):
            jid = "_selftest-%s" % kind
            jd = os.path.join(ROOT, "jobs", jid)
            shutil.rmtree(jd, ignore_errors=True)
            os.makedirs(os.path.join(jd, "work"))
            job = {"jobId": jid, "source": {"kind": kind, "pdf": "source/x.pdf"},
                   "trim": {"w": 420, "h": 297}, "bleedMm": 3}
            json.dump(job, open(os.path.join(jd, "job.json"), "w", encoding="utf-8"))
            ps = os.path.join(jd, "work", "p.ps")
            with open(ps, "w", encoding="ascii") as fh:
                fh.write("<< /PageSize [%.4f %.4f] >> setpagedevice\n"
                         "0 0 0 0.2 setcmykcolor 0 0 100 100 rectfill showpage\n"
                         % (page_mm[0] * 72 / 25.4, page_mm[1] * 72 / 25.4))
            subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                            "-sDEVICE=pdfwrite", "-o", os.path.join(jd, "work", "cmyk.pdf"), ps],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run = subprocess.run([sys.executable, os.path.join(HERE, "01d-scale-final.py"), jid],
                                 capture_output=True, text=True)
            shutil.rmtree(jd, ignore_errors=True)
            return run

        generic_rgb = "/System/Library/ColorSync/Profiles/Generic RGB Profile.icc"
        if os.path.exists(generic_rgb):
            jid = "selftest-rgb-not-srgb"
            jd = os.path.join(ROOT, "jobs", jid)
            shutil.rmtree(jd, ignore_errors=True)
            os.makedirs(os.path.join(jd, "source"))
            ps = os.path.join(jd, "source", "src.ps")
            with open(ps, "w", encoding="ascii") as fh:
                fh.write("<< /PageSize [1207.559 858.898] >> setpagedevice\n"
                         "0.2 0.4 0.3 setrgbcolor 0 0 1207.559 858.898 rectfill showpage\n")
            raw_pdf = os.path.join(jd, "source", "src-base.pdf")
            subprocess.run([GS, "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
                            "-sDEVICE=pdfwrite", "-o", raw_pdf, ps], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            writer = PdfWriter(clone_from=PdfReader(raw_pdf))
            page = writer.pages[0]
            rgb_stream = DecodedStreamObject()
            rgb_stream.set_data(open(generic_rgb, "rb").read())
            rgb_stream[NameObject("/N")] = NumberObject(3)
            res = page["/Resources"].get_object()
            cs = res.get("/ColorSpace")
            cs = cs.get_object() if cs is not None else DictionaryObject()
            res[NameObject("/ColorSpace")] = cs
            cs[NameObject("/DefaultRGB")] = ArrayObject(
                [NameObject("/ICCBased"), writer._add_object(rgb_stream)])
            writer.write(os.path.join(jd, "source", "src.pdf"))
            os.unlink(ps)
            os.unlink(raw_pdf)
            job = {"jobId": jid, "source": {"kind": "srgb-pdf", "pdf": "source/src.pdf"},
                   "trim": {"w": 420, "h": 297}, "bleedMm": 3,
                   "output": {"ai": jid + ".ai", "pdf": jid + ".pdf"},
                   "pdfExport": {"fileName": jid + ".pdf"}}
            json.dump(job, open(os.path.join(jd, "job.json"), "w", encoding="utf-8"))
            run = subprocess.run([os.path.join(HERE, "01-prepare.sh"), jid],
                                 cwd=ROOT, capture_output=True, text=True)
            shutil.rmtree(jd, ignore_errors=True)
            expect("sourceMode.nonSRGB", "fail" if run.returncode else "pass", "fail",
                   {"returncode": run.returncode,
                    "stderr": run.stderr.strip().splitlines()[-2:]})

        direct = geometry_case("srgb-pdf", (420, 297))
        expect("geometry.directWrongSize", "fail" if direct.returncode else "pass", "fail",
               {"returncode": direct.returncode, "stderr": direct.stderr.strip().splitlines()[-2:]})
        figma = geometry_case("figma", (568, 404))
        expect("geometry.figmaScale", "pass" if figma.returncode == 0 else "fail", "pass",
               {"returncode": figma.returncode})

        if MUTOOL:
            ind = gate3.independent_render_check(base, base, None, None)
            expect("independentRender.twoEngines", ind["status"], "pass")
        else:
            ind = gate3.independent_render_check(base, base, None, None)
            expect("independentRender.missingMuPDF", ind["status"], "pending")

    payload = {
        "ok": all(c["pass"] for c in checks.values()),
        "fixture": "generated PDFs in a temporary directory; no client artwork",
        "targetICC": {"path": os.path.relpath(TARGET, ROOT), "sha256": TARGET_SHA},
        "checks": checks,
        "counts": {"checks": len(checks),
                   "pass": sum(1 for c in checks.values() if c["pass"]),
                   "fail": sum(1 for c in checks.values() if not c["pass"])},
    }
    out = os.path.join(ROOT, "tests", "rca-regression-selftest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(json.dumps({"ok": payload["ok"], "counts": payload["counts"]}, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
