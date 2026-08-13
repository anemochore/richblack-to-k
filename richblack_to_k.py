#!/usr/bin/env python3
"""
richblack_to_k.py

PDF 안의 비트맵 이미지를 다음 규칙으로 처리한다.

[RGB 이미지]
- 프로파일이 없는 DeviceRGB는 sRGB로 간주한다.
- RGB -> Lab 변환 후 중성색/어두운 픽셀을 판별한다.
- 컬러 픽셀: 지정한 CMYK ICC로 정상 RGB -> CMYK 변환
- 중성 픽셀: 같은 CMYK ICC에서 원래 L*에 가장 가까운 K-only 값으로 치환
  => C=0, M=0, Y=0, K=matched_K
- 따라서 결과 이미지 전체는 CMYK가 된다.

[기존 CMYK 이미지]
- 지정한 CMYK ICC를 그 이미지의 기준 프로파일로 간주한다.
- 컬러 픽셀은 기존 CMYK 값을 그대로 보존한다.
- 중성/어두운 픽셀만 L*를 최대한 유지하는 K-only로 치환한다.

[안티에일리어싱 가장자리]
- 기본 중성/K-only 대상 픽셀의 주변 1px를 추가로 검사한다.
- 주변 픽셀은 --chroma에 --edge-chroma-multiplier를 곱한 C*ab 기준을 사용한다.
- 밝기 기준은 1차 판정과 동일하게 --max-l을 사용한다.
- 이미지 전체에 느슨한 chroma 기준을 적용하지 않고, 확실한 중성 픽셀 주변에만 적용한다.

[투명성]
- /SMask 등 기존 이미지 딕셔너리를 유지하고 색상 스트림만 다시 쓴다.
- 따라서 soft mask 참조는 그대로 보존된다.

[페이지 제외]
- --exclude-pages "1,5,10-12" 형식으로 도비라 등 처리하지 않을 페이지를 지정할 수 있다.
- 하나의 이미지 xref가 제외 페이지와 처리 페이지 양쪽에서 재사용되면,
  제외 페이지까지 바뀌는 것을 막기 위해 그 xref 전체를 건너뛴다.
- 논리 페이지가 아니라 물리 페이지 기준이다.

[Output Intent]
- 기존 PDF에 usable Output Intent ICC가 있으면 CMYK 변환 기준으로 사용할 수 있다.
- 결과 PDF에 새 Output Intent를 추가하거나 기존 Output Intent를 수정하지 않는다.
- 기본 ICC는 profiles/JapanColor2001Coated.icc 이다.

필요 패키지:
    python -m pip install -U pymupdf numpy pillow

기본 사용:
    python richblack_to_k.py input.pdf output.pdf

먼저 검사만:
    python richblack_to_k.py input.pdf --dry-run

일부 페이지 제외(물리 페이지 기준):
    python richblack_to_k.py input.pdf output.pdf --exclude-pages "1,9,57,105-107"

다른 ICC를 명시적으로 사용:
    python richblack_to_k.py input.pdf output.pdf --icc output.icc

중성 판정을 더 엄격하게:
    --chroma 4

더 밝은 안티에일리어싱까지 처리:
    --max-l 95

가장자리의 chroma 허용 배수 조정:
    --edge-chroma-multiplier 1.7

기본값:
    --chroma 6
    --max-l 95
    --edge-chroma-multiplier 1.7
"""

from __future__ import annotations

import argparse
import hashlib
import io
import math
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        raise SystemExit(
            "PyMuPDF가 필요합니다.\n"
            "설치: python -m pip install -U pymupdf numpy pillow"
        )

try:
    import numpy as np
except ImportError:
    raise SystemExit(
        "NumPy가 필요합니다.\n"
        "설치: python -m pip install -U pymupdf numpy pillow"
    )

try:
    from PIL import Image, ImageCms
except ImportError:
    raise SystemExit(
        "Pillow가 필요합니다.\n"
        "설치: python -m pip install -U pymupdf numpy pillow"
    )


REF_RE = re.compile(r"(\d+)\s+0\s+R")
ICCBASED_RE = re.compile(r"/ICCBased\s+(\d+)\s+0\s+R")
MIN_PYMUPDF = (1, 21, 0)

# 검정/회색 획의 안티에일리어싱 가장자리를 추가로 잡기 위한 내부 기본값.
# 일반 판정(--chroma / --max-l)으로 잡힌 픽셀의 바로 주변만 적용한다.
EDGE_RADIUS = 1

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ICC_PATH = SCRIPT_DIR / "profiles" / "JapanColor2001Coated.icc"


def check_pymupdf_version() -> None:
    version = getattr(pymupdf, "pymupdf_version_tuple", None)
    if version is not None and tuple(version) < MIN_PYMUPDF:
        raise SystemExit(
            "PyMuPDF 1.21.0 이상이 필요합니다.\n"
            "업데이트: python -m pip install -U pymupdf"
        )


def parse_page_spec(spec: str | None, page_count: int) -> set[int]:
    """
    '1,5,10-12' -> 0-based page set.
    """
    if not spec:
        return set()

    result: set[int] = set()

    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            a, b = part.split("-", 1)
            try:
                start = int(a)
                end = int(b)
            except ValueError:
                raise ValueError(f"잘못된 페이지 범위: {part}")

            if start > end:
                start, end = end, start

            if start < 1 or end > page_count:
                raise ValueError(
                    f"페이지 범위가 PDF 범위를 벗어남: {part} "
                    f"(PDF는 1-{page_count}쪽)"
                )

            result.update(range(start - 1, end))

        else:
            try:
                page = int(part)
            except ValueError:
                raise ValueError(f"잘못된 페이지 번호: {part}")

            if page < 1 or page > page_count:
                raise ValueError(
                    f"페이지 번호가 PDF 범위를 벗어남: {page} "
                    f"(PDF는 1-{page_count}쪽)"
                )

            result.add(page - 1)

    return result


def xref_from_value(value: str) -> int | None:
    m = REF_RE.search(value or "")
    return int(m.group(1)) if m else None


def object_text_for_value(doc, kind: str, value: str) -> str:
    if kind == "xref":
        ref = xref_from_value(value)
        if ref:
            try:
                return doc.xref_object(ref, compressed=False)
            except Exception:
                pass
    return value or ""


def load_image_embedded_icc(doc, image_xref: int) -> bytes | None:
    """
    이미지 /ColorSpace가 [/ICCBased n 0 R]이면 ICC stream 반환.
    RGB 이미지의 embedded ICC가 있을 때 source profile로 사용한다.
    """
    try:
        kind, value = doc.xref_get_key(image_xref, "ColorSpace")
    except Exception:
        return None

    text = object_text_for_value(doc, kind, value)
    m = ICCBASED_RE.search(text)
    if not m:
        return None

    icc_xref = int(m.group(1))

    try:
        data = doc.xref_stream(icc_xref)
        return bytes(data) if data else None
    except Exception:
        return None


def load_output_intent_icc(doc) -> bytes | None:
    """
    PDF Catalog의 /OutputIntents에서 첫 DestOutputProfile ICC를 반환한다.
    """
    try:
        catalog_xref = doc.pdf_catalog()
        kind, value = doc.xref_get_key(catalog_xref, "OutputIntents")
    except Exception:
        return None

    text = object_text_for_value(doc, kind, value)

    for output_intent_xref in (int(x) for x in REF_RE.findall(text)):
        try:
            profile_kind, profile_value = doc.xref_get_key(
                output_intent_xref,
                "DestOutputProfile",
            )
        except Exception:
            continue

        if profile_kind != "xref":
            continue

        profile_xref = xref_from_value(profile_value)
        if not profile_xref:
            continue

        try:
            data = doc.xref_stream(profile_xref)
            if data:
                return bytes(data)
        except Exception:
            pass

    return None


def read_icc(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except Exception as exc:
        raise SystemExit(f"ICC 파일 읽기 실패: {path}\n{exc}")


def profile_from_bytes(data: bytes):
    return ImageCms.ImageCmsProfile(io.BytesIO(data))


def profile_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]




def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    """
    boolean mask를 8방향으로 radius px 확장한다.

    SciPy 같은 추가 패키지 없이 NumPy만 사용한다.
    radius=0이면 원본 mask를 그대로 반환한다.
    """
    if radius <= 0:
        return mask.copy()

    result = mask.copy()
    height, width = result.shape

    for _ in range(radius):
        padded = np.pad(
            result,
            ((1, 1), (1, 1)),
            mode="constant",
            constant_values=False,
        )

        expanded = np.zeros_like(result)
        for dy in range(3):
            for dx in range(3):
                expanded |= padded[
                    dy : dy + height,
                    dx : dx + width,
                ]

        result = expanded

    return result


class ColorEngine:
    """
    출력 CMYK ICC를 중심으로 필요한 transform을 재사용한다.
    """

    def __init__(self, output_cmyk_icc: bytes):
        self.output_bytes = output_cmyk_icc
        self.output_profile = profile_from_bytes(output_cmyk_icc)
        self.lab_profile = ImageCms.createProfile("LAB")
        self.srgb_profile = ImageCms.createProfile("sRGB")

        # 출력 CMYK -> Lab
        self.output_cmyk_to_lab = ImageCms.buildTransformFromOpenProfiles(
            self.output_profile,
            self.lab_profile,
            "CMYK",
            "LAB",
            renderingIntent=1,  # relative colorimetric
        )

        # sRGB -> Lab / CMYK
        self.srgb_to_lab = ImageCms.buildTransformFromOpenProfiles(
            self.srgb_profile,
            self.lab_profile,
            "RGB",
            "LAB",
            renderingIntent=1,
        )

        self.srgb_to_cmyk = ImageCms.buildTransformFromOpenProfiles(
            self.srgb_profile,
            self.output_profile,
            "RGB",
            "CMYK",
            renderingIntent=1,
        )

        self.rgb_profile_cache: dict[str, tuple[object, object]] = {}

        # C0 M0 Y0 K0..255 -> L* LUT
        strip = np.zeros((1, 256, 4), dtype=np.uint8)
        strip[0, :, 3] = np.arange(256, dtype=np.uint8)

        lab_img = ImageCms.applyTransform(
            Image.fromarray(strip, mode="CMYK"),
            self.output_cmyk_to_lab,
        )
        lab = np.asarray(lab_img, dtype=np.uint8)[0]

        # Pillow LAB의 L 채널은 0..255.
        self.k_lbyte = lab[:, 0].astype(np.int16)

        # 각 L byte(0..255)에 가장 가까운 K sample을 미리 계산.
        all_l = np.arange(256, dtype=np.int16)[:, None]
        dist = np.abs(all_l - self.k_lbyte[None, :])
        self.lbyte_to_k = np.argmin(dist, axis=1).astype(np.uint8)

    def _rgb_transforms_for_profile(self, icc_bytes: bytes):
        key = hashlib.sha256(icc_bytes).hexdigest()

        if key not in self.rgb_profile_cache:
            src = profile_from_bytes(icc_bytes)

            to_lab = ImageCms.buildTransformFromOpenProfiles(
                src,
                self.lab_profile,
                "RGB",
                "LAB",
                renderingIntent=1,
            )

            to_cmyk = ImageCms.buildTransformFromOpenProfiles(
                src,
                self.output_profile,
                "RGB",
                "CMYK",
                renderingIntent=1,
            )

            self.rgb_profile_cache[key] = (to_lab, to_cmyk)

        return self.rgb_profile_cache[key]

    def rgb_to_lab_and_cmyk(
        self,
        rgb: np.ndarray,
        embedded_rgb_icc: bytes | None,
    ):
        image = Image.fromarray(rgb, mode="RGB")

        if embedded_rgb_icc:
            try:
                to_lab, to_cmyk = self._rgb_transforms_for_profile(
                    embedded_rgb_icc
                )
                source_label = (
                    "embedded RGB ICC "
                    f"{profile_fingerprint(embedded_rgb_icc)}"
                )
            except Exception:
                # ICCBased이지만 Pillow/LittleCMS가 사용할 수 없는 경우
                # sRGB fallback.
                to_lab = self.srgb_to_lab
                to_cmyk = self.srgb_to_cmyk
                source_label = "sRGB fallback"
        else:
            to_lab = self.srgb_to_lab
            to_cmyk = self.srgb_to_cmyk
            source_label = "sRGB assumed"

        lab = np.asarray(
            ImageCms.applyTransform(image, to_lab),
            dtype=np.uint8,
        )

        cmyk = np.asarray(
            ImageCms.applyTransform(image, to_cmyk),
            dtype=np.uint8,
        ).copy()

        return lab, cmyk, source_label

    def cmyk_to_lab(self, cmyk: np.ndarray):
        image = Image.fromarray(cmyk, mode="CMYK")
        return np.asarray(
            ImageCms.applyTransform(image, self.output_cmyk_to_lab),
            dtype=np.uint8,
        )

    def neutral_mask_and_k(
        self,
        lab: np.ndarray,
        chroma_threshold: float,
        max_lstar: float,
        edge_chroma_multiplier: float,
    ):
        lbyte = lab[..., 0]
        lstar = lbyte.astype(np.float32) * (100.0 / 255.0)

        # Pillow LAB: a*, b*는 signed 8-bit 값이 uint8로 표현된다.
        a = lab[..., 1].view(np.int8).astype(np.float32)
        b = lab[..., 2].view(np.int8).astype(np.float32)

        chroma2 = a * a + b * b

        # 1차 판정: 사용자가 지정한 일반 중성/밝기 조건.
        base_mask = (
            (chroma2 <= chroma_threshold * chroma_threshold)
            & (lstar <= max_lstar)
        )

        # 2차 판정: 확실한 중성 픽셀의 바로 주변만 chroma 조건을
        # 조금 완화하여 검사한다. 밝기 기준은 1차와 동일하게 유지한다.
        near_base = dilate_mask(base_mask, EDGE_RADIUS)
        edge_chroma = chroma_threshold * edge_chroma_multiplier
        edge_mask = (
            near_base
            & ~base_mask
            & (chroma2 <= edge_chroma * edge_chroma)
            & (lstar <= max_lstar)
        )

        mask = base_mask | edge_mask

        new_k = self.lbyte_to_k[lbyte]

        return mask, new_k


def choose_output_icc(doc, icc_path: Path | None):
    """
    출력 CMYK ICC 선택 우선순위:
    1. --icc로 명시한 ICC
    2. PDF의 Output Intent ICC
    3. profiles/JapanColor2001Coated.icc

    이 함수는 ICC 선택에만 관여한다. PDF의 Output Intent는 추가/수정하지 않는다.
    """
    if icc_path is not None:
        if not icc_path.exists():
            raise ValueError(f"ICC 파일이 없습니다: {icc_path}")

        output_icc = read_icc(icc_path)
        try:
            engine = ColorEngine(output_icc)
        except Exception as exc:
            raise ValueError(
                f"지정한 ICC를 CMYK 출력 프로파일로 사용할 수 없습니다:\n"
                f"{icc_path}\n{exc}"
            ) from exc

        return output_icc, engine, str(icc_path)

    output_intent_icc = load_output_intent_icc(doc)
    if output_intent_icc:
        try:
            engine = ColorEngine(output_intent_icc)
            return output_intent_icc, engine, "PDF Output Intent"
        except Exception as exc:
            print(
                "[경고] PDF의 Output Intent ICC를 CMYK 출력 프로파일로 "
                f"사용할 수 없어 기본 ICC로 대체합니다: {exc}",
                file=sys.stderr,
            )

    if not DEFAULT_ICC_PATH.exists():
        raise ValueError(
            "PDF에 사용할 수 있는 Output Intent ICC가 없고 "
            f"기본 ICC 파일도 없습니다: {DEFAULT_ICC_PATH}"
        )

    output_icc = read_icc(DEFAULT_ICC_PATH)
    try:
        engine = ColorEngine(output_icc)
    except Exception as exc:
        raise ValueError(
            f"기본 ICC를 CMYK 출력 프로파일로 사용할 수 없습니다:\n"
            f"{DEFAULT_ICC_PATH}\n{exc}"
        ) from exc

    return output_icc, engine, str(DEFAULT_ICC_PATH)

def pixmap_to_array(pix, components: int) -> np.ndarray:
    """
    Pixmap stride를 고려하여 H x W x components 배열 반환.
    base image 색 채널만 취하고 alpha가 붙어 있어도 제외한다.
    """
    if pix.colorspace is None or pix.colorspace.n != components:
        raise ValueError(
            f"예상 색상 성분 {components}, 실제 "
            f"{getattr(pix.colorspace, 'n', None)}"
        )

    raw = np.frombuffer(pix.samples, dtype=np.uint8)
    expected = pix.height * pix.stride

    if raw.size != expected:
        raise ValueError(
            f"Pixmap buffer 크기 불일치: {raw.size} != {expected}"
        )

    rows = raw.reshape(pix.height, pix.stride)
    useful = rows[:, : pix.width * pix.n]
    pixels = useful.reshape(pix.height, pix.width, pix.n)

    return pixels[..., :components].copy()


def collect_image_xrefs(doc):
    images = {}
    inline_occurrences = 0

    for page_no in range(doc.page_count):
        page = doc[page_no]

        try:
            infos = page.get_image_info(xrefs=True)
        except Exception as exc:
            print(
                f"[경고] {page_no + 1}쪽 이미지 정보 읽기 실패: {exc}",
                file=sys.stderr,
            )
            continue

        for info in infos:
            xref = int(info.get("xref", 0) or 0)

            if xref == 0:
                inline_occurrences += 1
                continue

            item = images.setdefault(
                xref,
                {
                    "pages": set(),
                    "occurrences": 0,
                    "has_mask": False,
                    "bpcs": set(),
                },
            )

            item["pages"].add(page_no)
            item["occurrences"] += 1
            item["has_mask"] = (
                item["has_mask"] or bool(info.get("has-mask", False))
            )

            if info.get("bpc") is not None:
                item["bpcs"].add(int(info["bpc"]))

    return images, inline_occurrences


def format_pages(pages: set[int], max_items: int = 8) -> str:
    ordered = sorted(p + 1 for p in pages)

    if len(ordered) <= max_items:
        return ",".join(map(str, ordered))

    head = ",".join(map(str, ordered[:max_items]))
    return f"{head},…(+{len(ordered) - max_items})"


def rewrite_as_device_cmyk(
    doc,
    xref: int,
    cmyk: np.ndarray,
) -> None:
    """
    이미지 stream을 8-bit raw CMYK로 교체한다.

    유지:
      Width / Height / SMask / Interpolate 등 기존 dictionary 항목

    변경:
      ColorSpace -> /DeviceCMYK
      BitsPerComponent -> 8
      stream -> 새 CMYK samples

    삭제/무효화:
      Decode / DecodeParms
      (새 stream은 이미 정상 CMYK sample이므로 과거 decode 규칙 불필요)
    """
    raw = np.ascontiguousarray(cmyk, dtype=np.uint8).tobytes()

    # PyMuPDF는 update_stream()에서 필요할 경우 FlateDecode로 압축한다.
    doc.update_stream(xref, raw, compress=True)

    doc.xref_set_key(xref, "ColorSpace", "/DeviceCMYK")
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "Decode", "null")
    doc.xref_set_key(xref, "DecodeParms", "null")



def check_output_path_ready(output_path: Path) -> tuple[bool, str | None]:
    """
    실제 처리를 시작하기 전에 출력 경로에 저장할 수 있는지 확인한다.

    - 출력 폴더에 임시 파일을 만들 수 있는지 검사한다.
    - 기존 출력 파일이 있으면 Windows에서는 배타적으로 열어 다른
      프로그램이 파일을 사용 중인지 확인한다.
    - 다른 OS에서는 가능한 범위에서 비차단 파일 잠금을 시도한다.

    이 검사는 처리 도중 다른 프로그램이 파일을 새로 여는 경우까지
    막지는 못하므로 저장 직전에도 한 번 더 호출한다.
    """
    try:
        parent = output_path.resolve().parent
    except Exception:
        parent = output_path.parent.resolve()

    if not parent.exists():
        return False, f"출력 폴더가 없습니다: {parent}"

    if not parent.is_dir():
        return False, f"출력 경로의 상위 경로가 폴더가 아닙니다: {parent}"

    # 새 파일을 만들 수 있는 폴더인지 먼저 확인한다.
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".richblack_write_test_",
            dir=str(parent),
        )
        os.close(fd)
        os.unlink(temp_name)
        temp_name = None
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        return False, f"출력 폴더에 파일을 쓸 수 없습니다: {parent}\n{exc}"

    if not output_path.exists():
        return True, None

    if not output_path.is_file():
        return False, f"출력 경로가 일반 파일이 아닙니다: {output_path}"

    if os.name == "nt":
        # Windows: share mode 0으로 기존 파일을 열어 배타적 접근이
        # 가능한지 확인한다. Acrobat 등 다른 프로그램이 파일을 잡고
        # 있으면 ERROR_SHARING_VIOLATION(32) 등이 발생한다.
        try:
            import ctypes
            from ctypes import wintypes

            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x00000080
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE

            handle = create_file(
                str(output_path.resolve()),
                GENERIC_READ | GENERIC_WRITE,
                0,  # 배타적 접근: 다른 열린 핸들과 공유하지 않음
                None,
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )

            if handle == INVALID_HANDLE_VALUE:
                error_code = ctypes.get_last_error()
                if error_code in (32, 33):
                    return (
                        False,
                        "출력 파일이 다른 프로그램에서 열려 있거나 "
                        f"잠겨 있습니다: {output_path}",
                    )
                return (
                    False,
                    f"출력 파일에 쓸 수 없습니다: {output_path} "
                    f"(Windows 오류 {error_code})",
                )

            kernel32.CloseHandle(handle)
            return True, None

        except Exception as exc:
            return False, f"출력 파일 상태 확인 실패: {output_path}\n{exc}"

    # Windows 외 환경의 보조 검사.
    try:
        with output_path.open("r+b") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except ImportError:
                pass
    except OSError as exc:
        return False, f"출력 파일에 쓸 수 없습니다: {output_path}\n{exc}"

    return True, None

def process_pdf(
    input_path: Path,
    output_path: Path | None,
    icc_path: Path | None,
    chroma_threshold: float,
    max_lstar: float,
    edge_chroma_multiplier: float,
    exclude_spec: str | None,
    dry_run: bool,
) -> int:
    check_pymupdf_version()

    if not input_path.exists():
        print(f"입력 PDF가 없습니다: {input_path}", file=sys.stderr)
        return 2

    if not dry_run and output_path is None:
        print("실제 변환에는 output.pdf가 필요합니다.", file=sys.stderr)
        return 2

    if output_path is not None:
        try:
            if input_path.resolve() == output_path.resolve():
                print(
                    "원본 보호를 위해 입력/출력 PDF는 같을 수 없습니다.",
                    file=sys.stderr,
                )
                return 2
        except FileNotFoundError:
            pass

    if not dry_run:
        assert output_path is not None
        ready, reason = check_output_path_ready(output_path)
        if not ready:
            print(reason, file=sys.stderr)
            print(
                "출력 파일 상태를 확인한 뒤 다시 실행하세요. "
                "PDF 처리는 시작하지 않았습니다.",
                file=sys.stderr,
            )
            return 2

    try:
        doc = pymupdf.open(str(input_path))
    except Exception as exc:
        print(f"PDF 열기 실패: {exc}", file=sys.stderr)
        return 2

    try:
        if getattr(doc, "needs_pass", False):
            print("암호화된 PDF는 처리하지 않습니다.", file=sys.stderr)
            return 2

        try:
            output_icc, engine, icc_source = choose_output_icc(
                doc,
                icc_path,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        try:
            excluded_pages = parse_page_spec(
                exclude_spec,
                doc.page_count,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        print(f"입력: {input_path}")
        print(f"페이지: {doc.page_count}")
        print(f"출력 CMYK ICC: {icc_source}")
        print(f"중성 조건: C*ab <= {chroma_threshold:g}")
        print(f"밝기 조건: L* <= {max_lstar:g}")
        print(
            "가장자리 조건: 1px 이내, "
            f"C*ab <= {chroma_threshold * edge_chroma_multiplier:g} "
            f"(--chroma x {edge_chroma_multiplier:g}), "
            f"L* <= {max_lstar:g}"
        )
        print("DeviceRGB source: embedded ICC가 없으면 sRGB로 간주")
        print("실행: 검사만" if dry_run else "실행: 변환")

        if excluded_pages:
            print(
                "제외 페이지: "
                + format_pages(excluded_pages, max_items=30)
            )

        images, inline_occurrences = collect_image_xrefs(doc)

        print(f"표시되는 이미지 XObject: {len(images)}개")

        if inline_occurrences:
            print(
                f"inline image: {inline_occurrences}회 "
                "(xref가 없어 이 버전에서는 제외)"
            )

        stats = defaultdict(int)
        total_pixels = 0
        neutral_pixels = 0
        applied_konly_pixels = 0

        for xref, meta in sorted(images.items()):
            pages = meta["pages"]

            if excluded_pages:
                included_uses = pages - excluded_pages
                excluded_uses = pages & excluded_pages

                if not included_uses:
                    stats["excluded"] += 1
                    if dry_run:
                        print(
                            f"[SKIP] xref {xref}: 제외 페이지에서만 사용, "
                            f"pages {format_pages(pages)}"
                        )
                    continue

                if excluded_uses and included_uses:
                    # 같은 xref를 고치면 제외 페이지에서도 바뀐다.
                    stats["shared_with_excluded"] += 1
                    print(
                        f"[SKIP] xref {xref}: 제외/처리 페이지에 동시에 재사용됨 "
                        f"(전체 pages {format_pages(pages)}). "
                        f"제외 페이지 보호를 위해 건너뜀."
                    )
                    continue

            if meta["bpcs"] and meta["bpcs"] != {8}:
                stats["non8"] += 1
                if dry_run:
                    print(
                        f"[SKIP] xref {xref}: "
                        f"bpc={sorted(meta['bpcs'])}, "
                        f"pages {format_pages(pages)}"
                    )
                continue

            try:
                pix = pymupdf.Pixmap(doc, xref)
            except Exception as exc:
                stats["error"] += 1
                print(
                    f"[ERROR] xref {xref}: Pixmap 생성 실패: {exc}",
                    file=sys.stderr,
                )
                continue

            if pix.colorspace is None:
                stats["unsupported"] += 1
                if dry_run:
                    print(
                        f"[SKIP] xref {xref}: 색공간 없음/image mask, "
                        f"pages {format_pages(pages)}"
                    )
                continue

            cs_n = pix.colorspace.n
            cs_name = getattr(pix.colorspace, "name", "?")
            image_total = pix.width * pix.height

            if cs_n == 3:
                stats["rgb"] += 1

                try:
                    rgb = pixmap_to_array(pix, 3)

                    embedded_rgb_icc = load_image_embedded_icc(
                        doc, xref
                    )

                    lab, output_cmyk, source_label = (
                        engine.rgb_to_lab_and_cmyk(
                            rgb,
                            embedded_rgb_icc,
                        )
                    )

                    mask, new_k = engine.neutral_mask_and_k(
                        lab,
                        chroma_threshold,
                        max_lstar,
                        edge_chroma_multiplier,
                    )

                except Exception as exc:
                    stats["error"] += 1
                    print(
                        f"[ERROR] xref {xref}: RGB 처리 실패: {exc}",
                        file=sys.stderr,
                    )
                    continue

                count = int(np.count_nonzero(mask))
                neutral_pixels += count
                total_pixels += image_total

                # 컬러 부분은 ICC RGB->CMYK 결과,
                # 중성 부분만 K-only로 덮어쓴다.
                output_cmyk[..., 0][mask] = 0
                output_cmyk[..., 1][mask] = 0
                output_cmyk[..., 2][mask] = 0
                output_cmyk[..., 3][mask] = new_k[mask]

                ratio = (
                    count / image_total * 100.0
                    if image_total
                    else 0.0
                )

                if dry_run or count:
                    smask = ", SMask" if meta["has_mask"] else ""
                    tag = "SCAN" if dry_run else "RGB→CMYK"
                    print(
                        f"[{tag}] xref {xref}: RGB, "
                        f"neutral {count:,}/{image_total:,} "
                        f"({ratio:.3f}%), "
                        f"pages {format_pages(pages)}, "
                        f"{source_label}{smask}"
                    )

                if not dry_run:
                    try:
                        rewrite_as_device_cmyk(
                            doc,
                            xref,
                            output_cmyk,
                        )
                        stats["modified_rgb"] += 1
                        if count:
                            stats["modified_rgb_konly"] += 1
                            applied_konly_pixels += count
                    except Exception as exc:
                        stats["error"] += 1
                        print(
                            f"[ERROR] xref {xref}: "
                            f"RGB→CMYK stream 저장 실패: {exc}",
                            file=sys.stderr,
                        )

            elif cs_n == 4:
                stats["cmyk"] += 1

                try:
                    cmyk = pixmap_to_array(pix, 4)
                    lab = engine.cmyk_to_lab(cmyk)

                    mask, new_k = engine.neutral_mask_and_k(
                        lab,
                        chroma_threshold,
                        max_lstar,
                        edge_chroma_multiplier,
                    )

                except Exception as exc:
                    stats["error"] += 1
                    print(
                        f"[ERROR] xref {xref}: CMYK 처리 실패: {exc}",
                        file=sys.stderr,
                    )
                    continue

                count = int(np.count_nonzero(mask))
                neutral_pixels += count
                total_pixels += image_total

                ratio = (
                    count / image_total * 100.0
                    if image_total
                    else 0.0
                )

                if dry_run or count:
                    smask = ", SMask" if meta["has_mask"] else ""
                    tag = "SCAN" if dry_run else "CMYK"
                    print(
                        f"[{tag}] xref {xref}: CMYK, "
                        f"neutral {count:,}/{image_total:,} "
                        f"({ratio:.3f}%), "
                        f"pages {format_pages(pages)}"
                        f"{smask}"
                    )

                if not dry_run and count:
                    result = cmyk.copy()
                    result[..., 0][mask] = 0
                    result[..., 1][mask] = 0
                    result[..., 2][mask] = 0
                    result[..., 3][mask] = new_k[mask]

                    try:
                        rewrite_as_device_cmyk(
                            doc,
                            xref,
                            result,
                        )
                        stats["modified_cmyk"] += 1
                        applied_konly_pixels += count
                    except Exception as exc:
                        stats["error"] += 1
                        print(
                            f"[ERROR] xref {xref}: "
                            f"CMYK stream 저장 실패: {exc}",
                            file=sys.stderr,
                        )

            else:
                stats["unsupported"] += 1

                if dry_run:
                    print(
                        f"[SKIP] xref {xref}: "
                        f"지원하지 않는 색공간 "
                        f"({cs_name}, n={cs_n}), "
                        f"pages {format_pages(pages)}"
                    )

        print()
        print("=== 결과 ===")
        print("검사 결과")
        print(f"  RGB 이미지: {stats['rgb']}개")
        print(f"  기존 CMYK 이미지: {stats['cmyk']}개")
        print(f"  지원하지 않는 색공간: {stats['unsupported']}개")
        print(f"  8 bpc가 아니어서 건너뜀: {stats['non8']}개")
        print(f"  제외 페이지 전용 이미지: {stats['excluded']}개")
        print(
            "  제외/처리 페이지에 공유되어 건너뜀: "
            f"{stats['shared_with_excluded']}개"
        )
        print(f"  오류: {stats['error']}개")
        print(f"  검사한 픽셀: {total_pixels:,}")

        if dry_run:
            print(f"  K-only 변환 예정 픽셀: {neutral_pixels:,}")
            print("PDF는 수정하지 않았습니다.")
            print()
            print(
                "주의: 실제 실행 시 처리 대상 RGB 이미지는 "
                "중성 픽셀 유무와 관계없이 지정 ICC의 CMYK로 변환됩니다."
            )
            return 0 if stats["error"] == 0 else 1

        assert output_path is not None

        # 처리 중 다른 프로그램이 출력 파일을 열었을 수도 있으므로
        # 실제 저장 직전에 한 번 더 확인한다.
        ready, reason = check_output_path_ready(output_path)
        if not ready:
            print(reason, file=sys.stderr)
            print(
                "이미지 처리는 끝났지만 출력 파일이 잠겨 있어 저장하지 못했습니다.",
                file=sys.stderr,
            )
            return 2

        try:
            doc.save(
                str(output_path),
                garbage=4,
                deflate=True,
            )
        except Exception as exc:
            print(f"PDF 저장 실패: {exc}", file=sys.stderr)
            return 2

        print()
        print("적용 결과")
        print(
            f"  RGB→CMYK 변환 완료: "
            f"{stats['modified_rgb']}개"
        )
        print(
            f"    └ 그중 K-only 적용: "
            f"{stats['modified_rgb_konly']}개"
        )
        print(
            f"  기존 CMYK에 K-only 적용: "
            f"{stats['modified_cmyk']}개"
        )
        print(f"  K-only 변환 완료 픽셀: {applied_konly_pixels:,}")
        print(f"출력: {output_path}")

        return 0 if stats["error"] == 0 else 1

    finally:
        doc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "PDF의 RGB 이미지는 지정 ICC로 CMYK 변환하고, "
            "RGB/CMYK 이미지의 어두운 중성 픽셀은 "
            "L*를 최대한 유지하는 K-only로 재분판합니다."
        )
    )

    parser.add_argument("input", type=Path, help="입력 PDF")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="출력 PDF (--dry-run이면 생략 가능)",
    )

    parser.add_argument(
        "--icc",
        type=Path,
        help=(
            "목표 CMYK ICC 프로파일. 미지정 시 PDF Output Intent를 "
            "시도하고, 사용할 수 없으면 "
            "profiles/JapanColor2001Coated.icc를 사용"
        ),
    )

    parser.add_argument(
        "--chroma",
        type=float,
        default=6.0,
        help=(
            "중성색으로 판단할 최대 C*ab "
            "(기본값: 6, 작을수록 엄격)"
        ),
    )

    parser.add_argument(
        "--max-l",
        type=float,
        default=95.0,
        help=(
            "K-only 변환할 최대 L* "
            "(기본값: 95, 높일수록 밝은 회색까지 포함)"
        ),
    )

    parser.add_argument(
        "--edge-chroma-multiplier",
        type=float,
        default=1.7,
        help=(
            "가장자리 1px에서 --chroma에 곱할 배수 "
            "(기본값: 1.7, 1이면 추가 완화 없음)"
        ),
    )

    parser.add_argument(
        "--exclude-pages",
        type=str,
        help=(
            '처리 제외 페이지(1-based). 예: "1,5,10-12"'
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="분석만 하고 PDF를 수정/저장하지 않음 (상세 출력 자동)",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not (0.0 <= args.chroma <= 181.0):
        parser.error("--chroma는 0~181 사이여야 합니다.")

    if not (0.0 <= args.max_l <= 100.0):
        parser.error("--max-l은 0~100 사이여야 합니다.")

    if args.edge_chroma_multiplier < 1.0:
        parser.error("--edge-chroma-multiplier는 1 이상이어야 합니다.")

    return process_pdf(
        input_path=args.input,
        output_path=args.output,
        icc_path=args.icc,
        chroma_threshold=args.chroma,
        max_lstar=args.max_l,
        edge_chroma_multiplier=args.edge_chroma_multiplier,
        exclude_spec=args.exclude_pages,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
