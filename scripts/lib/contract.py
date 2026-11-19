#!/usr/bin/env python3
"""Load and validate the single colour contract.

Every colour value used by conversion, audit, finalize and gate stages comes
from ``config/color-contract.json``. This module never falls back to a
library ICC constant. Missing files, hash mismatch or a spec mirror that
disagrees with the contract are hard errors.
"""

import hashlib
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(os.path.dirname(HERE))
REPO_ROOT = os.path.dirname(AGENT_ROOT)
CONTRACT_REL = os.path.join("config", "color-contract.json")
CONTRACT_PATH = os.path.join(AGENT_ROOT, CONTRACT_REL)


class ContractError(RuntimeError):
    pass


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def resolve(path):
    """Resolve a contract path against the repository, then the agent root."""
    if not path:
        raise ContractError("empty contract path")
    if os.path.isabs(path):
        return path
    candidate = os.path.join(REPO_ROOT, path)
    if os.path.exists(candidate) or path.startswith("print-agent/"):
        return candidate
    return os.path.join(AGENT_ROOT, path)


def load(path=None):
    target = path or CONTRACT_PATH
    if not os.path.exists(target):
        raise ContractError(
            "colour contract not found: %s. Copy config/color-contract.example.json "
            "to config/color-contract.json and fill it in." % target)
    contract = _read(target)
    if not contract.get("contractId") or not contract.get("version"):
        raise ContractError("colour contract missing contractId/version")
    for name, color in (contract.get("colors") or {}).items():
        cmyk = color.get("cmyk") or {}
        if len(cmyk.get("percent") or []) != 4 or len(cmyk.get("bytes8") or []) != 4:
            raise ContractError("colour %s needs four CMYK percent/byte values" % name)
        pct = [float(x) for x in cmyk["percent"]]
        byt = [int(x) for x in cmyk["bytes8"]]
        if any(abs(b - round(p * 2.55)) > 1 for p, b in zip(pct, byt)):
            raise ContractError("colour %s percent/byte values disagree: %s vs %s"
                                % (name, pct, byt))
        if "tacPercent" in cmyk and abs(sum(pct) - float(cmyk["tacPercent"])) > 0.2:
            raise ContractError("colour %s TAC disagrees with its CMYK sum" % name)
    acc = contract.get("acceptance") or {}
    brand_name = brand_color_name(contract)
    brand = (contract.get("colors") or {}).get(brand_name, {}).get("cmyk", {})
    if acc.get("expectedCmykBytes") and list(acc["expectedCmykBytes"]) != list(brand.get("bytes8") or []):
        raise ContractError("acceptance.expectedCmykBytes disagrees with %s" % brand_name)
    if acc.get("expectedPlateBytesInverted") and list(acc["expectedPlateBytesInverted"]) != [
            255 - int(x) for x in brand.get("bytes8", [])]:
        raise ContractError("acceptance.expectedPlateBytesInverted disagrees with %s" % brand_name)
    for key in ("source", "target", "renderRGB"):
        prof = (contract.get("profiles") or {}).get(key) or {}
        if not prof.get("path") or not prof.get("sha256"):
            raise ContractError("profile %s missing path/sha256" % key)
        resolved = resolve(prof["path"])
        if not os.path.exists(resolved):
            raise ContractError("profile %s not found: %s" % (key, resolved))
        actual = sha256(resolved)
        if actual != prof["sha256"]:
            raise ContractError("profile %s hash mismatch: %s != %s"
                                % (key, actual, prof["sha256"]))
        if prof.get("bytes") and os.path.getsize(resolved) != int(prof["bytes"]):
            raise ContractError("profile %s size mismatch" % key)
    return contract


def brand_color_name(contract):
    """Return the named brand colour. ``brandColor`` wins; otherwise the first
    colour with ``semanticRole=brand-surface``."""
    name = contract.get("brandColor")
    if name:
        if name not in (contract.get("colors") or {}):
            raise ContractError("brandColor %s is not in colors" % name)
        return name
    for key, entry in (contract.get("colors") or {}).items():
        if (entry.get("semanticRole") or "") == "brand-surface":
            return key
    raise ContractError("contract has no brandColor and no brand-surface colour")


def profile(contract, key):
    prof = contract["profiles"][key]
    return resolve(prof["path"]), prof


def color(contract, name):
    try:
        return contract["colors"][name]
    except KeyError:
        raise ContractError("colour %s is not in the contract" % name)


def cmyk_percent(contract, name):
    return list(color(contract, name)["cmyk"]["percent"])


def cmyk_bytes(contract, name):
    return list(color(contract, name)["cmyk"]["bytes8"])


def contract_sha(contract):
    return sha256(CONTRACT_PATH)


def apply_to_spec(spec, contract=None):
    """Return a copy of print-spec with colour fields filled from the contract.

    Legacy keys stay in the shape the existing scripts already read, but their
    values are contract values. A spec that carries a different non-null
    colour value is rejected instead of silently shadowing the contract.
    """
    contract = contract or load()
    out = json.loads(json.dumps(spec))
    print_spec = out.setdefault("print", {})
    target_path, target = profile(contract, "target")
    source_path, source = profile(contract, "source")
    render_path, render = profile(contract, "renderRGB")
    conversions = contract["conversion"]
    expected = {
        "iccProfile": target_path,
        "iccName": target["description"],
        "outputConditionID": target["outputConditionIdentifier"],
        "outputCondition": target["outputCondition"],
        "pdfxStandard": contract["pdf"]["standard"],
        "renderingIntent": conversions["renderingIntent"],
        "renderingIntentName": conversions["renderingIntentName"],
        "blackPointCompensation": conversions["blackPointCompensation"],
        "tacLimitPercent": contract["acceptance"]["tacLimitPercent"],
    }
    for key, value in expected.items():
        if key in ("outputConditionID", "pdfxStandard"):
            # A descriptive print-spec label is allowed; the contract value wins
            # and is the one written to the OutputIntent and the PDF/X id.
            print_spec[key] = value
            continue
        current = print_spec.get(key)
        if current not in (None, ""):
            same = current == value
            if key == "iccProfile":
                same = resolve(str(current)) == value
            if not same:
                raise ContractError("print-spec %s=%r disagrees with contract %r"
                                    % (key, current, value))
        print_spec[key] = value
    print_spec["colorContract"] = os.path.relpath(CONTRACT_PATH, AGENT_ROOT)
    print_spec["colorContractSha256"] = contract_sha(contract)
    print_spec["colorContractVersion"] = contract["version"]
    print_spec["sourceICC"] = source_path
    print_spec["sourceICCsha256"] = source["sha256"]
    print_spec["renderRGBICC"] = render_path
    print_spec["renderRGBICCsha256"] = render["sha256"]
    print_spec["pdfxStandardRevision"] = contract["pdf"]["standardRevision"]
    return out


def load_spec(path=None):
    spec_path = path or os.path.join(AGENT_ROOT, "config", "print-spec.json")
    return apply_to_spec(_read(spec_path))


if __name__ == "__main__":
    import sys
    c = load()
    if len(sys.argv) > 1 and sys.argv[1] == "get":
        node = c
        for part in sys.argv[2].split("."):
            node = node[int(part)] if isinstance(node, list) else node[part]
        print(node)
    else:
        print(json.dumps({
            "contractId": c["contractId"],
            "version": c["version"],
            "sha256": contract_sha(c),
            "target": profile(c, "target")[1]["sha256"],
            "source": profile(c, "source")[1]["sha256"],
        }, ensure_ascii=False))
