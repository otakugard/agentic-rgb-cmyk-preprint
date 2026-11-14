#!/bin/bash
# 01-prepare.sh <job-id>
#
# 把 Figma 导出的 RGB PDF 转成目标 ICC 的 CMYK PDF（ICC 见 config/print-spec.json）。
#
# 为什么不用 Illustrator 的「文档色彩模式 → CMYK」：
#   那条命令用的是当前工作 CMYK 空间，是个环境变量，换台机器、换个人点一下就可能不是 FOGRA39。
#   Ghostscript + ICC 是显式的：用哪个 profile 写在命令里，结果可复现、可检查。
#
# 输入：jobs/<id>/source/*.pdf（RGB 原件，只读）
# 输出：jobs/<id>/work/cmyk.pdf + prepare.json + source-audit.json

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPEC="$ROOT/config/print-spec.json"

[ $# -ge 1 ] || { echo "usage: 01-prepare.sh <job-id>" >&2; exit 64; }
JOB_ID="$1"
if [[ ! "$JOB_ID" =~ ^[a-z0-9][a-z0-9-]{2,60}$ ]]; then
  echo "invalid job id: '$JOB_ID'" >&2
  exit 64
fi

JOB="$ROOT/jobs/$JOB_ID"
[ -d "$JOB" ] || { echo "job dir not found: $JOB" >&2; exit 66; }

spec_get() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d=d[k] if isinstance(d,dict) else d[int(k)]
print(d if not isinstance(d,bool) else str(d).lower())' "$SPEC" "$1"
}

ICC="$(spec_get print.iccProfile)"
GS_BIN="$(spec_get ghostscript.binary)"
IMG_ENC="$(spec_get ghostscript.imageEncoding)"
INTENT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["print"].get("renderingIntent",1))' "$SPEC")"
BPC="$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1]))["print"].get("blackPointCompensation",True)).lower())' "$SPEC")"

[ -f "$ICC" ] || { echo "ICC profile missing: $ICC (run from repo root, or set an absolute path)" >&2; exit 78; }
command -v "$GS_BIN" >/dev/null 2>&1 || { echo "ghostscript not found: $GS_BIN" >&2; exit 78; }

case "$IMG_ENC" in
  flate) IMG_FILTER="FlateEncode" ;;
  dct)   IMG_FILTER="DCTEncode" ;;
  *) echo "config error: ghostscript.imageEncoding must be 'flate' or 'dct' (got '$IMG_ENC')" >&2; exit 78 ;;
esac

SRC_REL=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("source",{}).get("pdf",""))' "$JOB/job.json")
[ -n "$SRC_REL" ] || { echo "job.json missing source.pdf" >&2; exit 66; }
SRC="$JOB/$SRC_REL"
[ -f "$SRC" ] || { echo "source pdf not found: $SRC" >&2; exit 66; }

# source/ is immutable. Pin the first hash into job.json and refuse a later change.
RECORDED_SHA=$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("source") or {}).get("sha256") or "")' "$JOB/job.json")
ACTUAL_SHA=$(shasum -a 256 "$SRC" | awk '{print $1}')
if [ -n "$RECORDED_SHA" ] && [ "$RECORDED_SHA" != "$ACTUAL_SHA" ]; then
  echo "source hash mismatch: job.json records $RECORDED_SHA, file is $ACTUAL_SHA" >&2
  echo "source/ is immutable. Restore the original file or start a new job." >&2
  exit 65
fi
if [ -z "$RECORDED_SHA" ]; then
  python3 - "$JOB/job.json" "$ACTUAL_SHA" <<'PIN'
import json
import sys

path, sha = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
data.setdefault("source", {})["sha256"] = sha
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PIN
fi

WORK="$JOB/work"
mkdir -p "$WORK"
OUT="$WORK/cmyk.pdf"

# 源页数：多页不猜，直接拒绝（一个 nodeId 对应一页，多页说明节点选错了）
PAGES=$(python3 -c '
import re,sys
raw=open(sys.argv[1],"rb").read()
print(len(re.findall(rb"/Type\s*/Page[^s]", raw)))' "$SRC")
if [ "$PAGES" != "1" ]; then
  echo "source pdf has $PAGES page(s); this pipeline handles exactly 1 page per job" >&2
  exit 65
fi

# 审计源 PDF：提取实际嵌入 ICC 与真实颜色操作，不再靠 /DeviceCMYK 字符串猜。
# 这一步只读 source/，输出写 work/。
AUDIT_JSON="$(
  python3 "$ROOT/scripts/01a-source-audit.py" "$SRC" "$WORK"
)" || {
  RC=$?
  echo "source audit failed (rc=$RC): $AUDIT_JSON" >&2
  exit $RC
}
echo "$AUDIT_JSON"

# Illustrator imports PDF /S /Alpha clip masks using the mask fill as a
# luminosity value. Figma's binary clip rectangles are black, so the masked
# panels turn transparent. Whitening only the mask fill is opt-in; it keeps
# /S /Alpha and the clip shape intact. Without `maskPolicy: "whiten-alpha"`
# in job.json, the pipeline refuses the source.
MASK_POLICY=$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("maskPolicy") or ""))' "$JOB/job.json")
CLEAN_PDF="$WORK/source-white.pdf"
if [ "$MASK_POLICY" = "whiten-alpha" ]; then
  MASK_JSON="$WORK/mask-whiten.json"
  rm -f "$CLEAN_PDF" "$MASK_JSON" "$CLEAN_PDF.tmp"
  STRIP_JSON="$(python3 "$ROOT/scripts/01a2-whiten-alpha-masks.py" "$SRC" "$CLEAN_PDF" "$MASK_JSON")" || {
    RC=$?
    echo "alpha-mask rewrite failed (rc=$RC): $STRIP_JSON" >&2
    exit $RC
  }
  echo "$STRIP_JSON"
  CONVERT_SRC="$CLEAN_PDF"
  [ -s "$CONVERT_SRC" ] || { echo "cleaned source not written: $CONVERT_SRC" >&2; exit 70; }
else
  MASK_JSON="$WORK/mask-detection.json"
  rm -f "$CLEAN_PDF" "$WORK/mask-whiten.json" "$MASK_JSON" "$CLEAN_PDF.tmp"
  DETECT_JSON="$(python3 "$ROOT/scripts/01a2-whiten-alpha-masks.py" --detect "$SRC" "$MASK_JSON")" || {
    RC=$?
    echo "source contains /S /Alpha clip masks or an unsupported mask. Illustrator 28.4.1 reads alpha masks as black luminosity masks and blanks the panels." >&2
    echo "Refusing to rewrite them silently; see docs/limitations.md (Alpha cut masks)." >&2
    echo "Only set job.json maskPolicy=\"whiten-alpha\" after a written decision on the seam." >&2
    echo "$DETECT_JSON" >&2
    exit $RC
  }
  echo "$DETECT_JSON"
  CONVERT_SRC="$SRC"
fi

SRC_ICC="$WORK/source-icc.icc"
SOURCE_ICC_SHA=$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("icc") or {}).get("sha256") or "")' "$WORK/source-audit.json")
SOURCE_ICC_DESC=$(python3 -c 'import json,sys;print((json.load(open(sys.argv[1])).get("icc") or {}).get("description") or "")' "$WORK/source-audit.json")
SOURCE_KIND=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("kind") or "")' "$WORK/source-audit.json")

if [ "$SOURCE_KIND" != "rgb-icc" ] || [ ! -f "$SRC_ICC" ]; then
  echo "source is '$SOURCE_KIND', not an ICC-tagged RGB Figma export." >&2
  echo "mixed or already-CMYK input is refused by default; audit it first, do not re-separate blindly." >&2
  exit 65
fi

TARGET_SHA=$(shasum -a 256 "$ICC" | awk '{print $1}')
GS_VER=$("$GS_BIN" --version 2>/dev/null | head -1)

# Ghostscript 的实际转换参数。OverrideICC=false：保留嵌入源 ICC。
# RenderIntent=1：Relative Colorimetric；BlackPtComp=1：黑点补偿。
# 参数是否生效由 06-color-audit 的实测 CMYK 控制色块验证，不只看回显。
CONVERSION_JSON="$WORK/conversion.json"
python3 - "$CONVERSION_JSON" "$GS_BIN" "$GS_VER" "$ICC" "$TARGET_SHA" "$SRC_ICC" "$SOURCE_ICC_SHA" "$INTENT" "$BPC" "$IMG_FILTER" <<'PY'
import json, sys
out, gs, ver, icc, target_sha, src_icc, src_sha, intent, bpc, img_filter = sys.argv[1:12]
args = [
    "--permit-file-read=" + icc,
    "--permit-file-read=" + src_icc,
    "-q", "-dBATCH", "-dNOPAUSE", "-dSAFER",
    "-sDEVICE=pdfwrite",
    "-dPDFSETTINGS=/prepress",
    "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false", "-dDownsampleMonoImages=false",
    "-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
    "-dColorImageFilter=/" + img_filter, "-dGrayImageFilter=/" + img_filter,
    "-sColorConversionStrategy=CMYK",
    "-sOutputICCProfile=" + icc,
    "-dProcessColorModel=/DeviceCMYK",
    "-dOverrideICC=false",
    "-dRenderIntent=" + str(intent),
    "-dBlackPtComp=" + ("1" if str(bpc).lower() == "true" else "0"),
    "-sDefaultRGBProfile=" + src_icc,
]
payload = {
    "gsBinary": gs,
    "gsVersion": ver,
    "args": args,
    "targetICC": icc,
    "targetICCsha256": target_sha,
    "sourceICC": src_icc,
    "sourceICCsha256": src_sha,
    "overrideICC": False,
    "renderingIntent": int(intent),
    "renderingIntentName": {0: "perceptual", 1: "relative-colorimetric",
                            2: "saturation", 3: "absolute-colorimetric"}.get(int(intent), "unknown"),
    "blackPointCompensation": str(bpc).lower() == "true",
    "colorConversionStrategy": "CMYK",
    "processColorModel": "DeviceCMYK",
    "pdfSettings": "/prepress",
    "downsample": False,
    "imageFilter": img_filter,
    "blendConversionStrategy": "Ghostscript default; not overridden",
    "note": "Intent/BPC are recorded as requested. 06-color-audit verifies them on control patches.",
}
json.dump(payload, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY

LOG="$JOB/run.log"
{
  echo "== $(date '+%F %T') 01-prepare on $JOB_ID"
  echo "== icc: $ICC  sha256=$TARGET_SHA"
  echo "== source icc: $SRC_ICC  sha256=$SOURCE_ICC_SHA  desc=$SOURCE_ICC_DESC"
  echo "== intent=$INTENT bpc=$BPC override=false"
} >> "$LOG"

# 用 conversion.json 里的同一组参数执行；06 也用这组参数做控制色块。
GS_ARGS=()
while IFS= read -r line; do GS_ARGS+=("$line"); done < <(python3 -c 'import json,sys;[print(x) for x in json.load(open(sys.argv[1]))["args"]]' "$CONVERSION_JSON")
"$GS_BIN" "${GS_ARGS[@]}" -o "$OUT" "$CONVERT_SRC" >> "$LOG" 2>&1

RC=$?
if [ $RC -ne 0 ] || [ ! -f "$OUT" ]; then
  echo "ghostscript failed (rc=$RC), see $LOG" >&2
  exit 70
fi

python3 - "$JOB" "$SRC" "$CONVERT_SRC" "$OUT" "$ICC" "$CONVERSION_JSON" "$WORK/source-audit.json" "$SPEC" "$MASK_JSON" <<'PY'
import hashlib, json, os, re, sys

job, src, convert_src, out, icc, conv_path, audit_path, spec_path, mask_path = sys.argv[1:10]
raw = open(out, "rb").read()
mb = re.search(rb"/MediaBox\s*\[([^\]]*)\]", raw)
nums = [float(x) for x in mb.group(1).split()] if mb else []
conv = json.load(open(conv_path, encoding="utf-8"))
audit = json.load(open(audit_path, encoding="utf-8"))
mask = json.load(open(mask_path, encoding="utf-8"))

payload = {
    "ok": True,
    "step": "01-prepare",
    "input": os.path.relpath(src, job),
    "inputSha256": hashlib.sha256(open(src, "rb").read()).hexdigest(),
    "convertedFrom": os.path.relpath(convert_src, job),
    "convertedFromSha256": hashlib.sha256(open(convert_src, "rb").read()).hexdigest(),
    "maskReport": mask,
    "maskReportSha256": hashlib.sha256(open(mask_path, "rb").read()).hexdigest(),
    "output": os.path.relpath(out, job),
    "outputSha256": hashlib.sha256(raw).hexdigest(),
    "icc": icc,
    "iccSha256": conv["targetICCsha256"],
    "bytes": len(raw),
    "pages": len(re.findall(rb"/Type\s*/Page[^s]", raw)),
    "mediaBoxPt": nums,
    "pageMm": [round((nums[2] - nums[0]) * 25.4 / 72, 2), round((nums[3] - nums[1]) * 25.4 / 72, 2)] if len(nums) == 4 else None,
    "deviceRGB": len(re.findall(rb"/DeviceRGB", raw)),
    "deviceCMYK": len(re.findall(rb"/DeviceCMYK", raw)),
    "images": len(re.findall(rb"/Subtype\s*/Image", raw)),
    "sourceDeviceRGB": audit.get("structure", {}).get("deviceRGB", 0),
    "sourceDeviceCMYK": audit.get("structure", {}).get("deviceCMYK", 0),
    "sourceKind": audit.get("kind"),
    "sourceICC": audit.get("icc"),
    "sourceICCsha256": conv["sourceICCsha256"],
    "conversion": conv,
    "sourceAuditSha256": hashlib.sha256(open(audit_path, "rb").read()).hexdigest(),
    "configSha256": hashlib.sha256(open(spec_path, "rb").read()).hexdigest(),
}

with open(os.path.join(job, "prepare.json"), "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)

print(json.dumps({"ok": True, "output": payload["output"], "pageMm": payload["pageMm"],
                  "deviceRGB": payload["deviceRGB"], "deviceCMYK": payload["deviceCMYK"],
                  "bytes": payload["bytes"], "sourceICC": (payload.get("sourceICC") or {}).get("description"),
                  "intent": conv["renderingIntent"], "bpc": conv["blackPointCompensation"],
                  "maskConverted": len((payload.get("maskReport") or {}).get("converted") or [])},
                 ensure_ascii=False))
PY

RC=$?
if [ $RC -ne 0 ]; then
  echo "post-check failed" >&2
  exit 70
fi

# Always produce work/cmyk-final.pdf at trim + 2 x bleed. 07c reads only this
# file. With scaleInPdf, 02 also imports it and skips its own resize.
python3 "$ROOT/scripts/01d-scale-final.py" "$JOB_ID" || exit $?
