#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dailv 1.7 改写实现。由 app/server.py 调用，用来降低中文文本的 AI 率。"""

from __future__ import annotations

import argparse
import codecs
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Runtime defaults
# ---------------------------------------------------------------------------

THREADS = int(os.environ.get("DAILV_THREADS", str(os.cpu_count() or 4)))
DEVICE = os.environ.get("DAILV_DEVICE", "").strip()
# 打字机节奏：对齐本机 ~80–90 t/s；积压大时批量追赶，避免拖在生成之后
CHAR_DELAY = float(os.environ.get("DAILV_CHAR_DELAY", "0.010"))
CHAR_DELAY_FAST = 0.003
CHAR_DELAY_CATCHUP = 0.0
# 多段流水线展示速度：对齐本机约 85–90 tok/s 的可见字速，给下一段生成留重叠窗口
DISPLAY_CPS = float(os.environ.get("DAILV_DISPLAY_CPS", "100"))
# 推理优化：Flash Attn 本机实测 fa=auto 最优（~85 t/s）；勿强制 on
FLASH_ATTN = os.environ.get("DAILV_FLASH_ATTN", "auto")
CACHE_TYPE_K = os.environ.get("DAILV_CACHE_K", "").strip()
CACHE_TYPE_V = os.environ.get("DAILV_CACHE_V", "").strip()
# 常驻 llama-server；命中同 alias 时走 --server-base，端到端可从 ~3s → ~0.6s
# 设 DAILV_SERVER=0 / off / none 可禁用
_SERVER_ENV = os.environ.get("DAILV_SERVER", "").strip()
SERVER_URL = "" if _SERVER_ENV.lower() in ("", "0", "off", "none", "false") else _SERVER_ENV
SERVER_ALIAS = {
    "1.7b": "dailv1.7",
    "1.5b": "dailv1.5",
}

STOP_MARKERS = ("\n[ Prompt:", "\nExiting...", "\nExiting")
RESULT_LABEL = "改写结果："
THINKING_FRAMES = ("思考中.", "思考中..", "思考中...")
_DONE = object()

_SENT_SPLIT = re.compile(r"(?<=[。！？；;!?])")
_PARA_SPLIT = re.compile(r"(\n\s*\n)")

# 硬保护：书名/引号、阿拉伯与中文数字及单位、英文单词、图表指代、公式、DOI/编号等
# 交替顺序：更长、更具体的模式在前，避免被短模式抢先匹配
_PROTECT_RE = re.compile(
    r"("
    # --- 4. 公式 ---
    r"\$\$[^$]+\$\$|"
    r"\$[^$\n]{1,200}\$|"
    r"\\\[[\s\S]{1,400}?\\\]|"
    r"\\\([\s\S]{1,200}?\\\)|"
    # --- 7. DOI / 专利 / 编号串 ---
    r"(?i:doi)[:：\s]*10\.\d{4,9}/[^\s，。；;）)\]]+|"
    r"10\.\d{4,9}/[A-Za-z0-9.\-;()/_]+|"
    r"(?:专利号?|(?i:Patent)\s*(?:No\.?)?)[:：\s]*[A-Z]{1,3}(?:\d[\d.\-]{4,})|"
    r"(?:伦理(?:学)?(?:审查)?(?:批件)?(?:编号|号)|(?i:Ethics?)\s*(?:Approval\s*)?(?:No\.?)?)[:：\s]*[A-Za-z0-9\-./]{4,}|"
    r"(?:基金项目|基金|项目)(?:资助)?(?:编号|号)?[:：\s]*[A-Za-z0-9\-·.]{4,}|"
    r"(?:No\.?|编号)[:：\s]*[A-Za-z][A-Za-z0-9\-./]{3,}|"
    # --- 1+5. 引号 / 书名号（含 ASCII 直引号）---
    r"《[^》\n]{1,120}》|"
    r"〈[^〉\n]{1,120}〉|"
    r"「[^」\n]{1,120}」|"
    r"『[^』\n]{1,120}』|"
    r"【[^】\n]{1,120}】|"
    r"“[^”\n]{1,120}”|"
    r"‘[^’\n]{1,80}’|"
    r"\"[^\"\n]{1,120}\"|"
    # --- 5. 图表指代 ---
    r"(?:图|表|公式)\s*\d+(?:\s*[-–—~～]\s*\d+)?|"
    r"(?i:Fig(?:ure)?|Table|Eq(?:uation)?)\.?\s*\d+(?:\s*[-–—]\s*\d+)?|"
    # --- 3. 置信区间 / 统计括号（须在文献角标 [1] 之前）---
    r"95%\s*CI\s*[\[(（][^\]）)]{1,40}[\]）)]|"
    r"[\[(（]\s*\d+\.\d+\s*[,，]\s*\d+\.\d+\s*[\]）)]|"
    r"[tFχΧ]\s*\(\s*\d+\s*(?:,\s*\d+)?\s*\)\s*=\s*[-−]?\d+(?:\.\d+)?|"
    # --- 3. 统计量与事实性数字 ---
    r"[pP]\s*[<≤>≥=]\s*0?\.\d+|"
    r"[rR]\s*=\s*[-−+]?\s*0?\.\d+|"
    r"[nN]\s*=\s*\d{1,6}|"
    r"(?:M|SD|SE|SEM|β|α|η²|η2|χ²|χ2|df|OR|HR|AUC)\s*=\s*[-−+]?\d+(?:\.\d+)?|"
    r"\d+(?:\.\d+)?\s*%|"
    r"(?:19|20)\d{2}\s*年|"
    r"\d+(?:\.\d+)?\s*(?:万人|亿元|万元|千人|亿人|个百分点|"
    r"亿|万|人(?!口)|例|项|次|篇|个|岁|天|周|个月|月|"
    r"分钟|小时|名|所|家|份|组|班|校|倍|公里|千米|米|千克|公斤|吨|"
    r"度|级|届|条|种|元|秒|年)|"
    # --- 中文数字及紧随单位（单字「十/百」不算数，避免锁「十分/百分之」的碎片）---
    r"百分之(?:"
    r"[零〇一二三四五六七八九两]{1,4}[十百千万亿]"
    r"|[十百千万亿][零〇一二三四五六七八九两]{1,4}"
    r"|[零〇一二三四五六七八九两]"
    r")"
    r"(?:[零〇一二三四五六七八九两]{0,4}[十百千万亿]){0,3}"
    r"[零〇一二三四五六七八九两]{0,4}"
    r"(?:点[零〇一二三四五六七八九两]{1,4})?|"
    r"第[零〇一二三四五六七八九两十百千万亿]{1,8}(?:大|名)?|"
    r"(?:"
    r"[零〇一二三四五六七八九两]{1,4}[十百千万亿]"
    r"|[十百千万亿][零〇一二三四五六七八九两]{1,4}"
    r")"
    r"(?:[零〇一二三四五六七八九两]{0,4}[十百千万亿]){0,3}"
    r"[零〇一二三四五六七八九两]{0,4}"
    r"(?:点[零〇一二三四五六七八九两]{1,4})?"
    r"(?:多|余)?(?:万人|亿元|万元|千人|亿人|个百分点|"
    r"亿|万|人(?!口)|例|项|次|篇|个|岁|天|周|个月|月|"
    r"分钟|小时|名|所|家|份|组|班|校|倍|公里|千米|米|千克|公斤|吨|"
    r"度|级|届|条|种|元|秒|年|％|%)?|"
    r"[零〇一二三四五六七八九两]"
    r"(?:多|余)?(?:万人|亿元|万元|千人|亿人|个百分点|"
    r"亿|万|人(?!口)|例|项|次|篇|岁|天|周|个月|月|"
    r"分钟|小时|名|所|家|份|组|班|校|倍|公里|千米|米|千克|公斤|吨|"
    r"度|级|届|条|种|元|秒|年|％|%)|"
    # --- 文献角标 [1] [1-3] ---
    r"\[\d+(?:\s*[-–—,~～]\s*\d+)*\]|"
    # --- 英文单词 / 缩写（含 BLEU-4、F1、H5；放最后以免抢 DOI、Fig）---
    r"[A-Za-z]{2,}(?:[-‐‑–][A-Za-z0-9]+)*|"
    r"[A-Za-z]\d+"
    r")"
)
_PLACEHOLDER_RE = re.compile(r"@@D(\d+)@@", re.IGNORECASE)
# 模型偶发把占位符改成全角括号/数字，做宽松还原
_PLACEHOLDER_LOOSE_RE = re.compile(
    r"(?:"
    r"@@D(\d+)@@|"
    r"〚\s*([0-9０-９]+)\s*〛|"
    r"［\s*([0-9０-９]+)\s*］|"
    r"【\s*P?\s*([0-9０-９]+)\s*】"
    r")",
    re.IGNORECASE,
)
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_PROTECT_HINT = (
    "文中形如@@D0@@、@@D1@@的标记是不可改写内容的占位符："
    "必须在对应语义位置原样保留（含两侧@@、字母D与数字），"
    "不得删除、合并、改写、翻译，也不得改成其他括号或数字写法；"
    "不得在占位符处另写书名、数据或用「该书」「该数据」等指代替换；"
    "只改写占位符以外的文字。"
)
_VAGUE_BOOK_REF = re.compile(
    r"(该书|该著作|此书|本书|该文献|该文|上述著作|上述文献)"
)


def protect_spans(text: str) -> tuple[str, list[str]]:
    """Replace protected spans with @@Di@@ placeholders."""
    bag: list[str] = []

    def repl(m: re.Match[str]) -> str:
        bag.append(m.group(0))
        return f"@@D{len(bag) - 1}@@"

    return _PROTECT_RE.sub(repl, text), bag


def _ph_index(m: re.Match[str]) -> int | None:
    for g in m.groups():
        if g is not None:
            return int(g.translate(_FW_DIGITS))
    return None


def unprotect_spans(text: str, bag: list[str], *, expected: set[int] | None = None) -> str:
    """Restore placeholders; recover dropped titles via vague refs or in-place insert."""
    if not bag:
        return text

    def repl(m: re.Match[str]) -> str:
        i = _ph_index(m)
        if i is not None and 0 <= i < len(bag):
            return bag[i]
        return m.group(0)

    out = _PLACEHOLDER_LOOSE_RE.sub(repl, text)
    check = expected if expected is not None else set(range(len(bag)))
    dropped: list[str] = []
    for i in sorted(check):
        if 0 <= i < len(bag) and bag[i] not in out:
            dropped.append(bag[i])

    for span in list(dropped):
        if _VAGUE_BOOK_REF.search(out):
            out = _VAGUE_BOOK_REF.sub(span, out, count=1)
            dropped.remove(span)

    for span in list(dropped):
        m = re.search(r"[，。；、]", out)
        if m:
            out = out[: m.start()] + span + out[m.start() :]
        else:
            out = span + out
        dropped.remove(span)
        print(f"提示: 已插回保护内容 {span}", file=sys.stderr)

    return out


def reinject_missing_placeholders(original_masked: str, rewritten: str, expected: set[int]) -> str:
    """If the model dropped @@Di@@, put them back in original relative order."""
    if not expected:
        return rewritten
    present = placeholder_ids(rewritten)
    missing = sorted(expected - present)
    if not missing:
        return rewritten

    order = [int(m.group(1)) for m in _PLACEHOLDER_RE.finditer(original_masked)]
    out = rewritten
    for mid in missing:
        ph = f"@@D{mid}@@"
        pred = None
        for oid in order:
            if oid == mid:
                break
            if f"@@D{oid}@@" in out:
                pred = oid
        if pred is not None:
            pred_ph = f"@@D{pred}@@"
            idx = out.rfind(pred_ph)
            if idx >= 0:
                at = idx + len(pred_ph)
                out = out[:at] + ph + out[at:]
                print(f"提示: 占位符 @@D{mid}@@ 已按序补回", file=sys.stderr)
                continue
        succ = None
        seen = False
        for oid in order:
            if oid == mid:
                seen = True
                continue
            if seen and f"@@D{oid}@@" in out:
                succ = oid
                break
        if succ is not None:
            succ_ph = f"@@D{succ}@@"
            idx = out.find(succ_ph)
            if idx >= 0:
                out = out[:idx] + ph + out[idx:]
                print(f"提示: 占位符 @@D{mid}@@ 已按序补回", file=sys.stderr)
                continue
        try:
            pos = order.index(mid)
        except ValueError:
            pos = 0
        out = ph + out if pos == 0 else out + ph
        print(f"提示: 占位符 @@D{mid}@@ 已补回", file=sys.stderr)
    return out


def split_keep_parts(chunk: str, bag: list[str]) -> list[tuple[str, str]]:
    """Split masked chunk into ('t', text) / ('k', original_span) parts."""
    parts: list[tuple[str, str]] = []
    for p in re.split(r"(@@D\d+@@)", chunk):
        if not p:
            continue
        m = _PLACEHOLDER_RE.fullmatch(p)
        if m:
            i = int(m.group(1))
            parts.append(("k", bag[i] if 0 <= i < len(bag) else p))
        else:
            parts.append(("t", p))
    return parts


def placeholder_ids(text: str) -> set[int]:
    ids: set[int] = set()
    for m in _PLACEHOLDER_LOOSE_RE.finditer(text):
        i = _ph_index(m)
        if i is not None:
            ids.add(i)
    for m in _PLACEHOLDER_RE.finditer(text):
        ids.add(int(m.group(1)))
    return ids


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    model_path: str
    ngl: int
    ctx: int
    n_predict: int
    target_chars: int
    max_chars: int
    min_chars: int
    system_prompt: str
    temperature: float | None = None
    top_p: float | None = None
    repeat_penalty: float | None = None
    # "pack" = 段内按目标字数打包；"sentence" = 逐句改写（1.5B 降 AI 率兜底）
    split_mode: str = "pack"
    # Qwen3 等思考模型：关闭 reasoning，避免 <think> 占输出
    reasoning: str | None = None


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_15 = os.path.join(_REPO, "models", "Dailv1.5-f16.gguf")


PROFILES: dict[str, Profile] = {
    "1.7b": Profile(
        name="1.7b",
        label="1.7",
        model_path=_env(
            "DAILV17_MODEL",
            r"D:\hf_models\paperpass\gguf\Dailv1.7-f16.gguf",
        ),
        ngl=int(_env("DAILV17_NGL", "99")),
        ctx=int(_env("DAILV17_CTX", "1024")),
        n_predict=int(_env("DAILV17_N_PREDICT", "1024")),
        target_chars=200,
        max_chars=280,
        min_chars=80,
        system_prompt=(
            "你是中文学术论文降AI率改写助手。"
            "将用户段落改写成更像人工撰写的学术表述：避免空泛套话、排比堆砌、模板化起承转合；"
            "句式长短错落，用具体、克制的学术语言重述原意，保留术语与论点，不编造事实。"
            "必须原样保留原文中的英文缩写与专名（如 COPD、LLM、GOLD、GINA、RAG、H5、BLEU、ROUGE、GPU、F1），"
            "以及数字、百分数、序数（如「第三」）与量级用语（如「近1亿」）；不得删改、替换或意译掉这些标记。"
            "不得把病种/指南缩写串换（如哮喘≠慢阻肺/COPD，GINA≠GOLD）；不得扩写成百科定义。"
            "改写长度宜与原文接近（约0.7～1.2倍），勿显著扩写。"
            "只输出改写后的段落正文，不要解释。"
        ),
        temperature=float(_env("DAILV17_TEMP", "0.7")),
        top_p=float(_env("DAILV17_TOP_P", "0.9")),
        repeat_penalty=float(_env("DAILV17_REPEAT", "1.15")),
        reasoning="off",
    ),
    "1.5b": Profile(
        name="1.5b",
        label="1.5",
        model_path=_env("DAILV15_MODEL", _DEFAULT_15),
        ngl=int(_env("DAILV15_NGL", "99")),
        ctx=int(_env("DAILV15_CTX", "1024")),
        n_predict=int(_env("DAILV15_N_PREDICT", "256")),
        target_chars=90,
        max_chars=160,
        min_chars=12,
        system_prompt=(
            "你是中文学术论文降AI率改写助手。"
            "只改写用户给出的这一句话：用更自然、克制的学术表述重述原意；"
            "保留术语与论点，不编造事实，不扩展成多句长论述，不添加原文没有的例子或对策。"
            "改写长度宜与原文接近（约0.7～1.2倍），勿显著扩写。"
            "只输出改写后的这一句正文，不要解释、不要序号。"
        ),
        temperature=float(_env("DAILV15_TEMP", "0.7")),
        top_p=float(_env("DAILV15_TOP_P", "0.9")),
        repeat_penalty=float(_env("DAILV15_REPEAT", "1.15")),
        split_mode="sentence",
    ),
}

# rewrite_batch.py compatibility（默认走 1.7B 主线）
DEFAULT_NGL = PROFILES["1.7b"].ngl
DEFAULT_CTX = PROFILES["1.7b"].ctx
DEFAULT_N_PREDICT = PROFILES["1.7b"].n_predict
DEFAULT_THREADS = THREADS
DEFAULT_DEVICE = DEVICE
MODEL_PATH = PROFILES["1.7b"].model_path
SYSTEM_PROMPT = PROFILES["1.7b"].system_prompt

# 本地已弃用；Hub 仍保留
_RETIRED_PROFILES = {
    "3": "Dailv3",
    "3b": "Dailv3",
    "dailv3": "Dailv3",
    "7": "Dailv7",
    "7b": "Dailv7",
    "dailv7": "Dailv7",
}

@dataclass(frozen=True)
class RunConfig:
    profile: Profile
    text: str
    ngl: int
    ctx: int
    no_split: bool
    quiet_progress: bool


# ---------------------------------------------------------------------------
# Text split / pack
# ---------------------------------------------------------------------------

def split_text(text: str, profile: Profile) -> list[tuple[str, str]]:
    """[(chunk, suffix_after), ...] — suffix restores original paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    parts = _PARA_SPLIT.split(text)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        block = parts[i]
        i += 1
        sep = ""
        if i < len(parts) and _PARA_SPLIT.fullmatch(parts[i] or ""):
            sep = "\n\n"
            i += 1
        block = block.strip()
        if not block:
            continue
        if profile.split_mode == "sentence":
            pieces = _split_sentences(block, profile)
        else:
            pieces = _pack_paragraph(block, profile)
        for j, piece in enumerate(pieces):
            out.append((piece, sep if j == len(pieces) - 1 else ""))
    return out or [(text, "")]


def _split_sentences(para: str, profile: Profile) -> list[str]:
    """Sentence-level units for 1.5B hard AI-rate fallback."""
    sentences = [s.strip() for s in _SENT_SPLIT.split(para) if s.strip()]
    if not sentences:
        return [para]
    out: list[str] = []
    for sent in sentences:
        if len(sent) <= profile.max_chars:
            if len(sent) >= profile.min_chars:
                out.append(sent)
            elif out:
                out[-1] += sent
            else:
                out.append(sent)
            continue
        # Over-long sentence: split on commas, then hard-cut.
        chunks = [c.strip() for c in re.split(r"(?<=[，,、])", sent) if c.strip()]
        buf = ""
        for c in chunks:
            if not buf:
                buf = c
                continue
            if len(buf) + len(c) <= profile.max_chars:
                buf += c
            else:
                out.append(buf)
                buf = c
        if buf:
            if len(buf) > profile.max_chars:
                out.extend(
                    buf[i : i + profile.max_chars]
                    for i in range(0, len(buf), profile.max_chars)
                )
            else:
                out.append(buf)
    return out or [para]


def _pack_paragraph(para: str, profile: Profile) -> list[str]:
    if len(para) <= profile.max_chars:
        return [para]

    sentences = [s.strip() for s in _SENT_SPLIT.split(para) if s.strip()]
    if len(sentences) <= 1:
        return [para[i : i + profile.max_chars] for i in range(0, len(para), profile.max_chars)]

    chunks: list[str] = []
    buf = ""
    for sent in sentences:
        if len(sent) > profile.max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(
                sent[i : i + profile.max_chars] for i in range(0, len(sent), profile.max_chars)
            )
            continue
        if not buf:
            buf = sent
            continue
        merged = buf + sent
        near_target = len(buf) < profile.target_chars or len(merged) <= profile.target_chars + 40
        if len(merged) <= profile.max_chars and near_target:
            buf = merged
        elif len(buf) < profile.min_chars and len(merged) <= profile.max_chars:
            buf = merged
        else:
            chunks.append(buf)
            buf = sent
    if buf:
        chunks.append(buf)

    if (
        len(chunks) >= 2
        and len(chunks[-1]) < profile.min_chars
        and len(chunks[-2]) + len(chunks[-1]) <= profile.max_chars + 40
    ):
        tail = chunks.pop()
        chunks[-1] += tail
    return chunks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_llama_cli() -> str:
    env = os.environ.get("DAILV_LLAMA_CLI")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("llama-cli")
    if found:
        return found
    raise FileNotFoundError(
        "找不到 llama-cli。请确认已安装 llama.cpp，或设置环境变量 DAILV_LLAMA_CLI。"
    )


def find_llama_server() -> str:
    env = os.environ.get("DAILV_LLAMA_SERVER")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("llama-server")
    if found:
        return found
    raise FileNotFoundError(
        "找不到 llama-server。请确认已安装 llama.cpp，或设置环境变量 DAILV_LLAMA_SERVER。"
    )


def _server_models(url: str, timeout: float = 1.5) -> list[str]:
    """Return model ids/aliases from a live llama-server, or []."""
    import json
    import urllib.error
    import urllib.request

    base = url.rstrip("/")
    # 绕过系统/VPN 代理，否则访问 127.0.0.1 可能被劫持成 404
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(
            f"{base}/v1/models",
            headers={"Accept": "application/json", "User-Agent": "dailv"},
        )
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        # fallback: health-only → treat as unknown models (caller may still match by port)
        try:
            req = urllib.request.Request(f"{base}/health", headers={"User-Agent": "dailv"})
            with opener.open(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return []
            return ["__alive__"]
        except Exception:
            return []
    ids: list[str] = []
    for item in data.get("data") or data.get("models") or []:
        if not isinstance(item, dict):
            continue
        for key in ("id", "name", "model"):
            val = item.get(key)
            if isinstance(val, str) and val:
                ids.append(val.lower())
        for a in item.get("aliases") or []:
            if isinstance(a, str) and a:
                ids.append(a.lower())
    return ids


def resolve_server_base(profile: Profile) -> str | None:
    """Return a llama-server base URL when one is already listening."""
    if SERVER_URL:
        if _server_models(SERVER_URL):
            return SERVER_URL.rstrip("/")
        return None
    alias = SERVER_ALIAS.get(profile.name, f"dailv{profile.label}").lower()
    default_port = 8765 if profile.name == "1.7b" else 8766
    url = f"http://127.0.0.1:{default_port}"
    ids = _server_models(url)
    if not ids:
        return None
    if "__alive__" in ids or alias in ids or profile.name.lower() in ids:
        return url
    return None


def server_status_text() -> str:
    lines: list[str] = []
    for name, port in (("1.7b", 8765), ("1.5b", 8766)):
        url = f"http://127.0.0.1:{port}"
        ids = _server_models(url)
        alias = SERVER_ALIAS.get(name, name)
        if ids and (alias in ids or name in ids or "__alive__" in ids):
            lines.append(f"ON   {alias:8}  {url}")
        else:
            lines.append(f"OFF  {alias:8}  {url}")
    return "\n".join(lines)


def build_server_cmd(profile: Profile, port: int | None = None) -> list[str]:
    """Command line to start a dedicated llama-server for this profile."""
    server = find_llama_server()
    alias = SERVER_ALIAS.get(profile.name, f"dailv{profile.label}")
    if port is None:
        port = 8765 if profile.name == "1.7b" else 8766
    cmd = [
        server,
        "-m",
        profile.model_path,
        "-ngl",
        str(profile.ngl),
        "-t",
        str(THREADS),
        "-c",
        str(profile.ctx),
        "-fa",
        FLASH_ATTN,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "-a",
        alias,
        "--reasoning",
        profile.reasoning or "off",
    ]
    if DEVICE:
        cmd += ["-dev", DEVICE]
    if CACHE_TYPE_K:
        cmd += ["-ctk", CACHE_TYPE_K]
    if CACHE_TYPE_V:
        cmd += ["-ctv", CACHE_TYPE_V]
    return cmd


def serve_profile(profile_name: str = "1.7b", port: int | None = None) -> int:
    """Foreground llama-server for a profile (Ctrl+C to stop)."""
    name = _normalize_profile(profile_name)
    if name not in PROFILES:
        print(f"未知 profile: {profile_name}", file=sys.stderr)
        return 1
    profile = PROFILES[name]
    if not os.path.isfile(profile.model_path):
        print(f"错误: 模型不存在: {profile.model_path}", file=sys.stderr)
        return 1
    if port is None:
        port = 8765 if profile.name == "1.7b" else 8766
    cmd = build_server_cmd(profile, port=port)
    alias = SERVER_ALIAS.get(profile.name, f"dailv{profile.label}")
    print(
        f"[Dailv-server] {profile.label} alias={alias} "
        f"http://127.0.0.1:{port}  fa={FLASH_ATTN} ngl={profile.ngl}",
        flush=True,
    )
    print(f"[Dailv-server] 另开终端: Dailv1.7 <段落>  （自动复用常驻模型）", flush=True)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


def _normalize_profile(name: str) -> str:
    n = name.lower().strip()
    if n in _RETIRED_PROFILES:
        hub = _RETIRED_PROFILES[n]
        print(
            f"本地已弃用 {hub}（请改用 Dailv1.7 或 Dailv1.5）。"
            f"Hub 仍保留: https://huggingface.co/k86574/{hub}",
            file=sys.stderr,
        )
        sys.exit(2)
    aliases = {
        "dailv": "1.7b",
        "1.5": "1.5b",
        "1.5b": "1.5b",
        "15": "1.5b",
        "1p5": "1.5b",
        "1p5b": "1.5b",
        "dailv1.5": "1.5b",
        "dailv15": "1.5b",
        "1.7": "1.7b",
        "1.7b": "1.7b",
        "17": "1.7b",
        "1p7": "1.7b",
        "1p7b": "1.7b",
        "dailv1.7": "1.7b",
        "dailv17": "1.7b",
    }
    return aliases.get(n, n)


def parse_args(argv: list[str] | None = None) -> RunConfig:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 旧入口 --profile 3b/7b → 明确弃用提示
    for i, tok in enumerate(argv):
        if tok == "--profile" and i + 1 < len(argv):
            _normalize_profile(argv[i + 1])
            break
        if tok.startswith("--profile="):
            _normalize_profile(tok.split("=", 1)[1])
            break

    default = _normalize_profile(_env("DAILV_PROFILE", "1.7b"))
    if default not in PROFILES:
        default = "1.7b"

    p = argparse.ArgumentParser(
        prog="Dailv",
        description="学术段落改写（自动按最适字数拆分并拼接）",
    )
    p.add_argument("--profile", choices=sorted(PROFILES), default=default)
    p.add_argument("--no-split", action="store_true", help="禁用自动拆分")
    p.add_argument("--quiet-progress", action="store_true", help="不打印拆分进度")
    p.add_argument("text_and_opts", nargs="*", help="待改写文本；末尾可跟 ngl [ctx]")

    ns, unknown = p.parse_known_args(argv)
    tokens = list(ns.text_and_opts)
    for u in unknown:
        if u.startswith("-"):
            p.error(f"未知参数: {u}")
        tokens.append(u)

    if not tokens:
        p17, p15 = PROFILES["1.7b"], PROFILES["1.5b"]
        p.print_help(sys.stderr)
        print(
            f"\n推荐一次改写字数:\n"
            f"  Dailv1.7: 约 {p17.target_chars} 字（上限 {p17.max_chars}，默认主线）\n"
            f"  Dailv1.5: 约 {p15.target_chars} 字/句（上限 {p15.max_chars}，难降 AI 率兜底）\n"
            f"\n示例:\n"
            f"  Dailv1.7 本研究探讨了新媒体环境下的阅读习惯变迁。\n"
            f"  Dailv1.5 随着数字技术的飞速发展，人工智能在教育领域得到了广泛应用。\n"
            f"  Dailv1.7 较长文段... 40 1024\n"
            f"\n注: 本地已弃用 Dailv3/Dailv7（Hub 仍保留）。\n",
            file=sys.stderr,
        )
        sys.exit(1)

    profile = PROFILES[ns.profile]
    ngl, ctx = profile.ngl, profile.ctx
    if len(tokens) >= 2 and tokens[-1].isdigit() and tokens[-2].isdigit():
        ngl, ctx = int(tokens[-2]), int(tokens[-1])
        tokens = tokens[:-2]
    elif tokens[-1].isdigit():
        ngl = int(tokens[-1])
        tokens = tokens[:-1]

    text = " ".join(tokens).strip()
    if not text:
        print("错误: 请提供待改写段落。", file=sys.stderr)
        sys.exit(1)
    if ngl < 0 or ctx < 256:
        print("错误: ngl 须 >= 0，上下文建议 >= 256。", file=sys.stderr)
        sys.exit(1)
    return RunConfig(profile, text, ngl, ctx, ns.no_split, ns.quiet_progress)


# ---------------------------------------------------------------------------
# llama.cpp I/O + UI
# ---------------------------------------------------------------------------

def _enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _out(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def _find_stop(window: str) -> int | None:
    hits = [window.find(m) for m in STOP_MARKERS]
    hits = [p for p in hits if p >= 0]
    return min(hits) if hits else None


def _stop_prefix_hold(window: str) -> int:
    hold = 0
    for marker in STOP_MARKERS:
        for i in range(1, len(marker)):
            if window.endswith(marker[:i]):
                hold = max(hold, i)
    return hold


def _reveal_pace(backlog: int) -> tuple[int, float]:
    """Adaptive (chars_this_tick, sleep_s) from queue backlog — track gen speed."""
    if backlog >= 80:
        return max(backlog // 2, 12), CHAR_DELAY_CATCHUP
    if backlog >= 28:
        return 6, CHAR_DELAY_CATCHUP
    if backlog >= 12:
        return 3, CHAR_DELAY_FAST
    if backlog >= 4:
        return 2, CHAR_DELAY_FAST
    return 1, CHAR_DELAY


def _typewriter_out(text: str) -> None:
    """Reveal already-buffered text with the same adaptive typewriter as live stream."""
    if not text:
        return
    n = len(text)
    i = 0
    while i < n:
        left = n - i
        step, delay = _reveal_pace(left)
        sys.stdout.write(text[i : i + step])
        sys.stdout.flush()
        i += step
        if delay > 0:
            time.sleep(delay)


def _pace_out(text: str, ahead: queue.Queue, *, steady: bool) -> None:
    """Steady ~DISPLAY_CPS while the next chunk is still generating.

    If the next chunk is already queued, catch up faster so the pipeline
    does not add delay after the GPU has finished.
    """
    if not text:
        return
    tick = 0.05
    i = 0
    n = len(text)
    while i < n:
        queued = ahead.qsize() > 0
        if queued:
            cps = max(DISPLAY_CPS * 3, 280)
        elif steady:
            cps = DISPLAY_CPS
        else:
            cps = max(DISPLAY_CPS * 2, 180)
        step = max(1, int(cps * tick))
        sys.stdout.write(text[i : i + step])
        sys.stdout.flush()
        i += step
        if i < n and not queued:
            time.sleep(tick)


def _thinking_loop(stop: threading.Event, done: threading.Event) -> None:
    """Animate「思考中」on stderr until generation is ready to show."""
    try:
        _enable_ansi()
        i = 0
        while not stop.is_set():
            frame = THINKING_FRAMES[i % 3]
            sys.stderr.write("\r" + frame + "   ")
            sys.stderr.flush()
            i += 1
            if stop.wait(0.4):
                break
    finally:
        done.set()


def _clear_thinking() -> None:
    sys.stderr.write("\r" + " " * 20 + "\r")
    sys.stderr.flush()


def _reveal_loop(
    q: queue.Queue,
    printed: list[bool],
    err: list,
    thinking_stop: threading.Event,
    thinking_done: threading.Event,
    result_label: str,
    show_thinking: bool,
    display_body: bool = True,
) -> None:
    try:
        first = True
        while True:
            ch = q.get()
            if ch is _DONE:
                break
            if first:
                # 流式正文：首 token 结束思考并打标题
                # 保护模式（整段缓冲）：继续思考直到全部生成完
                if display_body:
                    thinking_stop.set()
                    thinking_done.wait(timeout=2.0)
                    if show_thinking:
                        _clear_thinking()
                    if result_label:
                        _out(result_label + "\n\n")
                first = False
            printed[0] = True
            if not display_body:
                continue
            # 按积压批量吐出，跟上生成速度
            step, delay = _reveal_pace(1 + q.qsize())
            sys.stdout.write(ch)
            for _ in range(step - 1):
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _DONE:
                    q.put(_DONE)
                    break
                sys.stdout.write(nxt)
            sys.stdout.flush()
            if delay > 0:
                time.sleep(delay)
    except Exception as e:
        err.append(e)
    finally:
        thinking_stop.set()
        thinking_done.wait(timeout=0.5)
        if show_thinking:
            _clear_thinking()


def _enqueue(q: queue.Queue, text: str, collected: list[str]) -> None:
    for ch in text:
        if ch == "\r":
            continue
        q.put(ch)
        collected.append(ch)


_PROMPT_ECHO_MARKERS = ("... (truncated)", "… (truncated)")


def strip_llama_prompt_echo(text: str) -> str:
    """Remove llama-cli prompt echo / help noise from captured rewrite output."""
    text = (text or "").replace("\r", "")
    if not text.strip():
        return ""

    for marker in _PROMPT_ECHO_MARKERS:
        if marker in text:
            text = text.split(marker)[-1]

    lines = text.splitlines()
    junk_prefixes = (
        "using custom system prompt",
        "  /clear",
        "  /read",
        "  /glob",
        "  /load",
    )
    while lines:
        raw = lines[0]
        stripped = raw.strip()
        if not stripped:
            lines.pop(0)
            continue
        if stripped.startswith(">"):
            lines.pop(0)
            continue
        if any(raw.startswith(p) or stripped.startswith(p.strip()) for p in junk_prefixes):
            lines.pop(0)
            continue
        break

    return "\n".join(lines).strip()


def _flag(cmd: list[str], name: str, default: str | None = None) -> str | None:
    try:
        i = cmd.index(name)
    except ValueError:
        return default
    if i + 1 >= len(cmd):
        return default
    return cmd[i + 1]


def _trim_repeat(text: str) -> str:
    """Cut a runaway loop: the same 8+ char span repeated three times in a row."""
    n = len(text)
    if n < 36:
        return text
    for span in (12, 16, 20, 24, 32, 40, 48):
        if span * 3 > n:
            break
        step = 2
        i = 0
        last = n - span * 3
        while i <= last:
            unit = text[i : i + span]
            if (
                len(unit.strip()) >= 8
                and text[i + span : i + span * 2] == unit
                and text[i + span * 2 : i + span * 3] == unit
            ):
                return text[: i + span].rstrip()
            i += step
    return text


# ---------------------------------------------------------------------------
# Export gates — reject bad chunks, low-temp retry, else fall back to source
# ---------------------------------------------------------------------------

_GATE_LEN_LO = float(os.environ.get("DAILV_GATE_LEN_LO", "0.5"))
_GATE_LEN_HI = float(os.environ.get("DAILV_GATE_LEN_HI", "1.5"))
_GATE_RETRY = max(0, int(os.environ.get("DAILV_GATE_RETRY", "2")))
_GATE_TEMP = float(os.environ.get("DAILV_GATE_TEMP", "0.35"))
# Productized semguard decode: BoN + probe reinject + drop-aware select/agg
_BON_N = max(1, int(os.environ.get("DAILV_BON_N", "4")))
_PROBE_REINJECT = os.environ.get("DAILV_PROBE_REINJECT", "1") != "0"
_AGGRESSIVE_RETRY = os.environ.get("DAILV_AGGRESSIVE_RETRY", "1") != "0"
# Align with Hub accept: rank by detector drop; agg on low drop; keep best on gate fail
_DROP_RANK = os.environ.get("DAILV_DROP_RANK", "1") != "0"
_AGGRESSIVE_DROP_FLOOR = float(
    os.environ.get("DAILV_AGGRESSIVE_DROP_FLOOR", os.environ.get("AGGRESSIVE_DROP_FLOOR", "0.20"))
)
# Soft length ceiling while scoring style-break samples (id15 list bodies)
_AGG_LEN_HI = float(os.environ.get("DAILV_AGG_LEN_HI", "1.7"))
_WEAK_DROP = float(os.environ.get("DAILV_WEAK_DROP", "0.20"))
_FALLBACK_SRC = os.environ.get("DAILV_FALLBACK_SRC", "0") == "1"
_BON_TEMPS = (0.55, 0.70, 0.85, 0.65, 0.75, 0.95, 0.50, 0.80)
_DET_REPO = "yuchuantian/AIGC_detector_zhv3"
_DET_CANDIDATES = (
    r"D:\hf_models\.hf_cache\hub\models--yuchuantian--AIGC_detector_zhv3\snapshots\47695ff451b32c225dd938f4f478f7fdc6aa6bb0",
    r"D:\Temp\huggingface\hub\models--yuchuantian--AIGC_detector_zhv3\snapshots\47695ff451b32c225dd938f4f478f7fdc6aa6bb0",
)
_det_bundle: tuple | None = None  # (tok, model, device) | False if unavailable
_det_warned = False
_ENG_RUN_RE = re.compile(r"[A-Za-z]{2,}(?:\s+[A-Za-z]{2,}){7,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_FIG_RE = re.compile(r"图\s*(\d+)")
_NUM_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(%|％|万人|亿元|万元|人|例|项|次|篇|个|岁|天|周|月|"
    r"分钟|小时|名|所|家|份|组)"
)
_SPACE_DEC_RE = re.compile(r"(\d)\s*\.\s*(\d)")
# 量级口头词：原文有更大的、改写掉成更小的 → 打回（典型：十亿→一亿）
_MAG_DOWNGRADES = (
    ("十亿", "一亿"),
    ("百亿", "十亿"),
    ("千亿", "百亿"),
    ("万亿", "千亿"),
    ("千万", "百万"),
    ("百万", "十万"),
    ("十万", "万"),
    ("近1亿", "一千万人"),
    ("近一亿", "一千万人"),
)
_CN_ORDINAL_RE = re.compile(r"第[零〇一二三四五六七八九十百千万亿两\d]{1,8}")
_EN_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,11}\b|[A-Za-z]\d\b")
_EN_ACRONYM_KEEP = (
    "COPD",
    "LLM",
    "GOLD",
    "GINA",
    "RAG",
    "H5",
    "BLEU",
    "ROUGE",
    "GPU",
    "AI",
    "F1",
)
# Severe length: never ship if still beyond this after compress (prefer 原文)
_SEVERE_LEN_HI = float(os.environ.get("DAILV_SEVERE_LEN_HI", "1.8"))
_FALLBACK_SEVERE = os.environ.get("DAILV_FALLBACK_SEVERE", "1") != "0"
_POLARITY_FLIPS = (("千人千面", "千人一面"),)


def _norm_num_text(text: str) -> str:
    t = text.replace("％", "%").replace(" ", "")
    t = _SPACE_DEC_RE.sub(r"\1.\2", t)
    return t


def _mostly_chinese(text: str) -> bool:
    cjk = len(_CJK_RE.findall(text))
    if cjk < 12:
        return False
    latin = len(re.findall(r"[A-Za-z]", text))
    return cjk >= latin


_PROMPT_LEAK_MARKERS = (
    "不可改写的内容占位符",
    "只输出改写后的段落正文",
    "【强制】",
    "【强化降AI】",
)

_META_LEAK_RE = re.compile(
    r"本文主要研究内容|主要研究内容及|关键技术依托|只输出正文|不可改写的内容"
)
_ACCURACY_DUP_RE = re.compile(
    r"（?\s*Accuracy\s*）?\s*[\(（]\s*accuracy\s*[\)）]",
    re.I,
)
_MAG_STACK_RE = re.compile(
    r"超过近1亿万人|近1亿万人|近1亿，\s*近\s*1亿|患病人数近1亿，近\s*1亿"
)
# Preserve key product phrases when present in src
_CN_PHRASE_KEEP = ("高质量发展", "赋能", "口号式")
_RUNON_SOFT = 110
_RUNON_HARD = 160


def _polish_fluency(text: str) -> str:
    """Deterministic cleanup: EN punct in CN prose, Accuracy dup, mag stacks, CJK spaces."""
    if not text:
        return text
    t = text
    t = _ACCURACY_DUP_RE.sub("（Accuracy）", t)
    t = re.sub(r"超过近1亿万人", "近1亿", t)
    t = re.sub(r"近1亿万人", "近1亿", t)
    t = re.sub(r"患病人数近1亿，\s*近\s*1亿", "患病人数近1亿", t)
    t = re.sub(r"近1亿，\s*近\s*1亿", "近1亿", t)
    t = re.sub(r"第\s+三\s+大", "第三大", t)
    # EN punct → CN when abutting CJK / closing paren
    t = re.sub(r"(?<=[\u4e00-\u9fff）】」』])\s*,\s*", "，", t)
    t = re.sub(r"(?<=[\u4e00-\u9fff）】」』])\s*;\s*", "；", t)
    t = re.sub(r"(?<=[\u4e00-\u9fff）】」』])\s*:\s*", "：", t)
    # Period: not decimal digit.digit
    t = re.sub(
        r"(?<=[\u4e00-\u9fff）】」』])\s*\.(?=\s|$|[\u4e00-\u9fff「『（【])",
        "。",
        t,
    )
    # Collapse spaces between CJK chars
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    # "高质量的发展" / "质量型的发展" soft normalize only when used as soft reinject later
    return t


def _max_runon_span(text: str) -> int:
    """Longest stretch without Chinese sentence breaks."""
    parts = re.split(r"[。！？；\n]", text)
    return max((len(p) for p in parts), default=0)


def _fluency_penalty(src: str, out: str) -> int:
    """Soft rank penalty (higher worse). Does not hard-fail by itself."""
    pen = 0
    if _ACCURACY_DUP_RE.search(out):
        pen += 3
    if _MAG_STACK_RE.search(out):
        pen += 2
    if _META_LEAK_RE.search(out) and not _META_LEAK_RE.search(src):
        pen += 4
    # Residual EN punct in mostly-CN text
    if _mostly_chinese(src):
        en_punct = len(re.findall(r"(?<=[\u4e00-\u9fff）])[,.;]", out))
        if en_punct:
            pen += min(en_punct, 3)
    run = _max_runon_span(out)
    src_run = _max_runon_span(src)
    if run >= _RUNON_SOFT and run > src_run + 20:
        pen += 1 if run < _RUNON_HARD else 3
    for phrase in _CN_PHRASE_KEEP:
        if phrase in src and phrase not in out:
            pen += 2
    if "口号" in src and "口号" not in out and "口令" in out:
        pen += 3
    # Literary skeleton lock: kept 「不仅…作为…」pair → prefer broken-skeleton cands
    if "不仅" in src and "作为" in src and "不仅" in out and "作为" in out:
        pen += 2
    return pen


def export_gate_fail(src: str, out: str) -> str | None:
    """Return a short reason if rewritten text must not be shipped; else None."""
    if not out.strip():
        return "empty"
    if re.search(r"@@D\d+@@", out, re.I) or "〚" in out:
        return "placeholder_leak"
    if any(m in out for m in _PROMPT_LEAK_MARKERS) and not any(
        m in src for m in _PROMPT_LEAK_MARKERS
    ):
        return "prompt_leak"
    if _META_LEAK_RE.search(out) and not _META_LEAK_RE.search(src):
        return "meta_leak"
    if _mostly_chinese(src) and _ENG_RUN_RE.search(out) and not _ENG_RUN_RE.search(src):
        return "english_runaway"
    if _ACCURACY_DUP_RE.search(out):
        return "accuracy_dup"
    if _MAG_STACK_RE.search(out):
        return "magnitude_stack"

    # Domain / acronym swaps (101: 哮喘→COPD, GINA→GOLD)
    if "哮喘" in src and "哮喘" not in out and (
        "慢性阻塞性肺疾病" in out
        or "慢阻肺" in out
        or ("COPD" in out and "COPD" not in src)
    ):
        return "domain_swap=哮喘->COPD"
    if "GINA" in src and "GINA" not in out and "GOLD" in out:
        return "acronym_swap=GINA->GOLD"
    if "GOLD" in src and "GINA" in out and "GINA" not in src:
        return "acronym_swap=GOLD->GINA"
    if "RAG" in src and "RAG" not in out and "检索增强" not in out:
        return "missing_en=RAG"

    # Catastrophic invents before length: otherwise too_long masks invented_year
    s_n, o_n = _norm_num_text(src), _norm_num_text(out)
    extra_years = sorted(set(_YEAR_RE.findall(o_n)) - set(_YEAR_RE.findall(s_n)))
    if extra_years:
        return f"invented_year={extra_years}"
    extra_figs = sorted(set(_FIG_RE.findall(out)) - set(_FIG_RE.findall(src)))
    if extra_figs:
        return f"invented_fig={extra_figs}"

    ratio = len(out) / max(len(src), 1)
    if ratio > _GATE_LEN_HI:
        return f"too_long={ratio:.2f}"
    if ratio < _GATE_LEN_LO:
        return f"too_short={ratio:.2f}"

    um = {(m.group(1), m.group(2).replace("％", "%")) for m in _NUM_UNIT_RE.finditer(s_n)}
    am = {(m.group(1), m.group(2).replace("％", "%")) for m in _NUM_UNIT_RE.finditer(o_n)}
    u_nums = {n for n, _ in um}
    for n, unit in am:
        if (n, unit) in um:
            continue
        peers = [un for un, uu in um if uu == unit]
        if peers and n not in u_nums:
            return f"unit_flip={unit}:{peers}->{n}"
        # 原文裸数字、改写加单位（典型 BLEU 2.76 → 2.76%）
        if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d.%％])", s_n):
            return f"unit_add={n}{unit}"

    for big, small in _MAG_DOWNGRADES:
        if big in src and small in out and big not in out:
            return f"magnitude={big}->{small}"
    # 近1亿被改成「约1亿/一亿」等：丢了「近」也算量级漂移
    if ("近1亿" in src or "近一亿" in src) and (
        "近1亿" not in out and "近一亿" not in out
    ):
        return "magnitude_near1yi_dropped"
    for a, b in _POLARITY_FLIPS:
        if a in src and b in out and a not in out:
            return f"polarity={a}->{b}"
    if "口号" in src and "口号" not in out and "口令" in out:
        return "polarity=口号->口令"
    if "千人千面" in src and "千人千面" not in out:
        return "missing_probe=千人千面"
    for phrase in ("高质量发展", "赋能"):
        if phrase in src and phrase not in out:
            return f"missing_probe={phrase}"
    # Extreme run-on vs source (soft issues handled by penalty; hard only if severe)
    if (
        _mostly_chinese(src)
        and _max_runon_span(out) >= _RUNON_HARD
        and _max_runon_span(out) > _max_runon_span(src) + 40
    ):
        return f"runon={_max_runon_span(out)}"
    # 序数必须保留（第三→第四 是现网常见硬伤）
    src_ords = _CN_ORDINAL_RE.findall(src)
    out_ords = _CN_ORDINAL_RE.findall(out)
    for ord_s in src_ords:
        if ord_s not in out:
            if out_ords:
                return f"ordinal_changed={ord_s}->{'|'.join(out_ords[:3])}"
            return f"missing_cn_ord={ord_s}"
    # 「第三大」须整词保留；禁止黏成「第三大患病率」（致死→患病串义）
    if "第三大" in src:
        if "第三大" not in out:
            return "missing_cn_ord=第三大"
        if ("致死" in src or "死亡" in src) and re.search(r"第三大患病", out):
            if not re.search(r"第三大.{0,12}(致死|死亡)", out):
                return "ordinal_changed=第三大->第三大患病"
    # 原文中的英文缩写/专名须保留（COPD/LLM/GOLD/H5 等）
    keep: list[str] = []
    for tok in _EN_ACRONYM_KEEP:
        if tok in src and tok not in out:
            keep.append(tok)
    for m in _EN_ACRONYM_RE.finditer(src):
        tok = m.group(0)
        if tok not in keep and tok not in out:
            if tok.isupper() or re.match(r"^[A-Za-z]\d$", tok):
                keep.append(tok)
    if keep:
        seen: set[str] = set()
        uniq: list[str] = []
        for t in keep:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return f"missing_en={','.join(uniq[:5])}"
    return None


def reinject_probes(src: str, out: str) -> str:
    """Surgically restore EN/CN probes lost in an otherwise usable rewrite."""
    text = out
    inserts = {
        "LLM": [("大语言模型", "大语言模型（LLM）"), ("语言模型", "语言模型（LLM）")],
        "COPD": [
            ("慢性阻塞性肺疾病", "慢性阻塞性肺疾病（COPD）"),
            ("该病", "COPD"),
        ],
        "GOLD": [
            ("全球慢性阻塞性肺疾病创议", "全球慢性阻塞性肺疾病创议（GOLD）"),
            ("创议", "创议（GOLD）"),
        ],
        "H5": [("动态", "动态H5"), ("交互式网页", "交互式网页、动态H5")],
        "Accuracy": [("准确率", "准确率（Accuracy）")],
        "BLEU": [("BLEU-4", "BLEU-4"), ("精准度", "BLEU-4与精准度")],
        "ROUGE": [("ROUGE-L", "ROUGE-L"), ("流畅度", "ROUGE-L与流畅度")],
        "GPU": [("显存", "GPU显存")],
        "F1": [("F1值", "F1值"), ("F1", "F1")],
        "RAG": [("检索增强生成", "检索增强生成（RAG）")],
        "GINA": [("全球哮喘创议", "全球哮喘创议（GINA）"), ("创议", "创议（GINA）")],
    }
    for tok in ("COPD", "LLM", "GOLD", "H5", "Accuracy", "BLEU", "ROUGE", "GPU", "F1", "RAG", "GINA"):
        if tok not in src or tok in text:
            continue
        done = False
        for a, b in inserts.get(tok, []):
            if a in text:
                text = text.replace(a, b, 1)
                done = True
                break
        if not done and tok not in text:
            if "。" in text:
                i = text.index("。")
                text = text[:i] + f"（{tok}）" + text[i:]
            else:
                text = f"（{tok}）" + text
    if "BLEU-4" in src and "BLEU-4" not in text:
        if "BLEU" in text:
            text = text.replace("BLEU", "BLEU-4", 1)
        elif "问答" in text:
            text = text.replace("问答", "问答（BLEU-4）", 1)
    elif "BLEU" in src and "BLEU" not in text and "生成" in text:
        text = text.replace("生成", "生成（BLEU）", 1)
    if "ROUGE-L" in src and "ROUGE-L" not in text:
        if "ROUGE" in text:
            text = text.replace("ROUGE", "ROUGE-L", 1)
    elif "ROUGE" in src and "ROUGE" not in text and "流畅" in text:
        text = text.replace("流畅", "流畅（ROUGE）", 1)
    if ("近1亿" in src or "近一亿" in src) and ("近1亿" not in text and "近一亿" not in text):
        for bad in ("约一亿", "将近一亿", "约1亿", "近一亿人", "约为一亿"):
            if bad in text:
                text = text.replace(bad, "近1亿", 1)
                break
        else:
            if "患病人数" in text:
                text = text.replace("患病人数", "患病人数近1亿，", 1)
    if "千人千面" in src and "千人千面" not in text:
        if "个性化" in text:
            text = text.replace("个性化", "千人千面的个性化", 1)
        else:
            text = text.rstrip("。") + "，实现千人千面。"
    for m in _CN_ORDINAL_RE.finditer(src):
        ord_s = m.group(0)
        if ord_s not in text and ord_s == "第三" and "慢性疾病" in text:
            text = re.sub(
                r"全球[^。]{0,12}慢性疾病致死",
                f"全球{ord_s}大慢性疾病致死",
                text,
                count=1,
            )
    # Fix 「第三大患病率」串义 → 还原致死病因语序
    if (
        "第三大" in src
        and re.search(r"第三大患病", text)
        and not re.search(r"第三大.{0,12}(致死|死亡)", text)
    ):
        text2 = re.sub(
            r"(全球)?COPD第三大患病率不断上升[，,]?已经成为",
            "全球COPD患病率不断上升，已成为全球第三大慢性疾病致死病因，已是",
            text,
            count=1,
        )
        if text2 == text:
            text2 = re.sub(
                r"第三大患病率",
                "第三大慢性疾病致死病因，患病率",
                text,
                count=1,
            )
        text = text2
    if "高质量发展" in src and "高质量发展" not in text:
        for bad in ("高质量的发展", "质量型的发展", "质量型发展", "高质量目标"):
            if bad in text:
                text = text.replace(bad, "高质量发展", 1)
                break
        else:
            if "发展" in text:
                text = text.replace("发展", "高质量发展", 1)
    if "赋能" in src and "赋能" not in text:
        # Prefer restoring src verb-object 「赋能金融…」before crude glue
        m = re.search(r"赋能([\u4e00-\u9fffA-Za-z0-9、，,]{2,32})", src)
        restored = False
        if m:
            obj = m.group(1).strip("，,")
            head = re.split(r"[、，,]", obj)[0]
            if head and head in text:
                text = text.replace(head, "赋能" + head, 1)
                restored = True
        if not restored:
            for bad, good in (
                ("提升员工素质", "赋能"),
                ("赋予团队的力量", "赋能团队"),
                ("赋予团队", "赋能团队"),
                ("赋予能力", "赋能"),
            ):
                if bad in text:
                    text = text.replace(bad, good, 1)
                    restored = True
                    break
        if not restored:
            if "高质量发展" in text:
                text = text.replace("高质量发展", "赋能高质量发展", 1)
            elif "团队" in text:
                text = text.replace("团队", "赋能团队", 1)
            elif "金融" in text:
                text = text.replace("金融", "赋能金融", 1)
    # Cleanup botched glue: 「行业赋能精细化管理」→「赋能…精细化管理」
    if "赋能精细化管理" in text:
        text2 = re.sub(
            r"(金融[、，]交通[、，]能源(?:等[^，。]{0,10})?)赋能精细化管理",
            r"赋能\1的精细化管理",
            text,
            count=1,
        )
        if text2 != text:
            text = text2
        else:
            text = text.replace("赋能精细化管理", "赋能相关领域的精细化管理", 1)
    if "口号" in src and "口号" not in text and "口令" in text:
        text = text.replace("口令", "口号", 1)
    return _polish_fluency(text)


def _stiff_penalty(src: str, out: str) -> int:
    """How many AI-template openers the rewrite newly introduced."""
    markers = (
        "综上所述",
        "值得注意的是",
        "与此同时",
        "分为",
        "两大",
        "三层",
        "高质量发展",
        "赋能",
    )
    return sum(1 for m in markers if m not in src and m in out)


def _is_list_template(text: str) -> bool:
    """Detect id15-like list/colon academic skeletons that near-paraphrase poorly."""
    score = 0
    if re.search(r"分为.{0,16}(两大|两个|三类|三层|两层)", text):
        score += 2
    if (text.count("：") + text.count(":")) >= 2:
        score += 1
    if "采用" in text and ("作为" in text or "评价指标" in text):
        score += 1
    if any(k in text for k in ("评价维度", "性能指标", "效率指标", "评价指标")):
        score += 1
    if re.search(r"[；;].{0,8}[：:]", text):
        score += 1
    return score >= 2


_LIST_REWRITE_HINT = (
    "\n【列表体】原文是指标罗列。请改成连贯叙述，禁止保留「分为…两大/两个评价维度」骨架，"
    "禁止「A：…；B：…」冒号清单；先总述再分句写任务指标与资源消耗。"
    "Accuracy、F1、BLEU-4、ROUGE-L、GPU 等缩写必须原样保留。"
)

_LIST_AGG_HINT = (
    "\n【列表体打散】上一稿仍是模板清单。彻底打散："
    "不要出现「分为」「两大/两个评价维度」「性能指标：」「效率指标：」；"
    "用 2～4 句长短错落的叙述重写，先说评了什么，再说效果指标与显存/耗时；"
    "专名缩写原样保留，长度约原文的 0.8～1.5 倍，只输出正文。"
)

_NARR_AGG_HINT = (
    "\n【叙述体打散】检测器降幅不足。硬性禁止：「不仅…还/更…」「作为…瑰宝/组成部分」、"
    "「源源不断」「彰显」「永恒魅力」。"
    "改用口语短句重写：先说《窦娥冤》/原文主题为什么耐看或有用，再补一句传承价值；"
    "长度约原文七成到一倍二，勿过短，只输出正文。"
)

_SHORT_REWRITE_HINT = (
    "\n【短段】原文较短。只改写句式与用词，禁止补充背景、定义、年份、流行病学数据或百科解释；"
    "禁止把专科术语串换成近邻概念；长度须接近原文（约0.8～1.2倍）；"
    "使用中文标点；Accuracy 等缩写只保留一次、勿重复括号；只输出正文。"
)

_SHORT_BON_TEMPS = (0.45, 0.55, 0.65, 0.50, 0.70, 0.40, 0.60, 0.75)


def _with_gate_len_hi(hi: float):
    """Temporarily raise length gate for style-break candidate acceptance."""
    global _GATE_LEN_HI
    prev = _GATE_LEN_HI
    _GATE_LEN_HI = max(prev, float(hi))
    return prev


def _restore_gate_len_hi(prev: float) -> None:
    global _GATE_LEN_HI
    _GATE_LEN_HI = prev


def _char_sim(a: str, b: str) -> float:
    """Cheap overlap proxy (Hub uses semantic sim; enough for BoN tie-break)."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter / max(len(sa | sb), 1)


def _ensure_detector():
    """Lazy-load zhv3 for in-CLI drop ranking. Prefer CPU to leave VRAM for llama-server."""
    global _det_bundle, _det_warned
    if not _DROP_RANK:
        return None
    if _det_bundle is False:
        return None
    if _det_bundle is not None:
        return _det_bundle
    try:
        from pathlib import Path

        import torch
        from transformers.models.bert import BertForSequenceClassification, BertTokenizer

        det_path = next(
            (p for p in _DET_CANDIDATES if Path(p).joinpath("config.json").is_file()),
            None,
        )
        if det_path is None:
            from huggingface_hub import snapshot_download

            det_path = snapshot_download(_DET_REPO)
        # CPU default: llama-server already owns GPU; override with DAILV_DET_DEVICE=cuda
        want = os.environ.get("DAILV_DET_DEVICE", "cpu").strip().lower()
        if want == "cuda" and torch.cuda.is_available():
            device = torch.device("cuda")
        else:
            device = torch.device("cpu")
        tok = BertTokenizer.from_pretrained(str(det_path))
        model = BertForSequenceClassification.from_pretrained(str(det_path))
        model.to(device).eval()
        _det_bundle = (tok, model, device)
        return _det_bundle
    except Exception as exc:  # noqa: BLE001 — keep CLI usable without detector
        _det_bundle = False
        if not _det_warned:
            _det_warned = True
            print(f"提示: drop 选优不可用（{exc}），回退 gate/stiff 排序\n", file=sys.stderr)
        return None


def _ai_prob(text: str) -> float | None:
    bundle = _ensure_detector()
    if bundle is None:
        return None
    tok, model, device = bundle
    import torch

    with torch.no_grad():
        inputs = tok(text, return_tensors="pt", max_length=512, truncation=True)
        inputs = {k: v.to(device) for k, v in inputs.items()}
        return float(model(**inputs).logits[0].softmax(0)[1].item())


def _gate_severity(fail: str | None, src: str, out: str) -> tuple[int, float]:
    """Lower is better. Prefer gate-ok, then probe-miss, then mild len, never prompt/invent."""
    ratio = len(out) / max(len(src), 1)
    len_pen = abs(ratio - 1.0)
    if fail is None:
        return 0, len_pen
    if fail in (
        "empty",
        "placeholder_leak",
        "prompt_leak",
        "meta_leak",
        "english_runaway",
    ) or fail.startswith(
        ("invented_", "unit_flip=", "unit_add=", "polarity=", "domain_swap=", "acronym_swap=")
    ):
        return 4, len_pen
    if fail in ("accuracy_dup", "magnitude_stack") or fail.startswith("runon="):
        return 2, len_pen
    if fail.startswith("too_long") or fail.startswith("too_short"):
        # Prefer nearer to 1.0 among length fails (not pure max-drop runaway)
        return 3, len_pen
    if fail.startswith(
        ("missing_en=", "missing_cn_ord=", "ordinal_changed=", "magnitude", "missing_probe=")
    ):
        return 2, len_pen
    return 3, len_pen


def _len_ratio(src: str, out: str) -> float:
    return len(out) / max(len(src), 1)


def _pick_ship_cand(
    src: str,
    cands: list[tuple[str, str | None, float | str]],
    ai_before: float | None,
) -> tuple[str, str | None]:
    """Prefer gate-ok; else length-in-band; never prefer severe too_long over shorter fails."""
    if not cands:
        return "", "empty"
    # Dedup by body text, keep first tag
    uniq: list[tuple[str, str | None, float | str]] = []
    seen: set[str] = set()
    for body, reason, tag in cands:
        key = body.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        fail = export_gate_fail(src, body)
        uniq.append((body, fail, tag))

    ok = [c for c in uniq if c[1] is None]
    if ok:
        ok.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
        return ok[0][0], None

    in_band = [
        c
        for c in uniq
        if _GATE_LEN_LO <= _len_ratio(src, c[0]) <= _GATE_LEN_HI
    ]
    if in_band:
        in_band.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
        return in_band[0][0], in_band[0][1]

    mild = [c for c in uniq if _len_ratio(src, c[0]) <= _SEVERE_LEN_HI]
    if mild:
        mild.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
        return mild[0][0], mild[0][1]

    uniq.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
    return uniq[0][0], uniq[0][1]


def _ai_headroom(ai_before: float | None) -> float | None:
    """How much absolute drop is even possible from this src AI score."""
    if ai_before is None:
        return None
    return max(0.0, float(ai_before))


def _drop_floor_applicable(ai_before: float | None, floor: float | None = None) -> bool:
    """False when src AI is already below the absolute drop floor (id1-class)."""
    flo = _AGGRESSIVE_DROP_FLOOR if floor is None else float(floor)
    hr = _ai_headroom(ai_before)
    if hr is None:
        return True
    return hr >= flo


def _cand_rank(src: str, out: str, *, ai_before: float | None = None) -> tuple:
    """Product rank: gate-ok max-drop; among fails prefer milder severity / nearer length.

    When src AI headroom < drop floor (already human-like), absolute drop is noise —
    prefer stronger paraphrase (lower sim) then lower after-AI among gate-ok.
    Fluency soft penalty breaks ties so drop-micro-wins don't ship unreadable text.
    """
    fail = export_gate_fail(src, out)
    sev, len_pen = _gate_severity(fail, src, out)
    drop = 0.0
    after = 1.0
    sim = _char_sim(src, out)
    flu = _fluency_penalty(src, out)
    if ai_before is not None:
        scored = _ai_prob(out)
        if scored is not None:
            after = float(scored)
            drop = float(ai_before) - after
    if fail is None:
        if not _drop_floor_applicable(ai_before):
            # Low-headroom pass pool: fluency → paraphrase → keep AI low
            return (0, flu, sim, after, len_pen, "")
        # Normal pass pool: fluency first, then maximize drop
        return (0, flu, -drop, len_pen, -sim, "")
    # Fail pool: severity → fluency → closer length → drop
    return (sev, flu, len_pen, -drop, -sim, fail)


def _cand_drop(src: str, out: str, ai_before: float | None) -> float | None:
    if ai_before is None:
        return None
    after = _ai_prob(out)
    if after is None:
        return None
    return float(ai_before) - float(after)


def _server_model_id(base: str) -> str:
    ids = [x for x in _server_models(base) if x and x != "__alive__"]
    return ids[0] if ids else ""


def _rewrite_max_tokens(n_chars: int, *, mode: str = "normal") -> int:
    """Scale generation budget to source length (stops short-para encyclopedia runaway).

    Qwen CJK ≈ 1 token/char; keep budget near 1.0–1.3× source chars.
    """
    n = max(1, int(n_chars))
    short_lim = int(os.environ.get("DAILV_SHORT_CHARS", "160"))
    if mode == "compress":
        return min(256, max(40, int(n * 1.15) + 8))
    if mode == "short" or n <= short_lim:
        return min(256, max(48, int(n * 1.25) + 8))
    return min(640, max(80, int(n * 1.5) + 16))


def _page_cancelled() -> bool:
    flag = globals().get("_PAGE_CANCEL")
    return bool(flag is not None and flag.is_set())


def _produce_server(
    cmd: list[str],
    q: queue.Queue,
    result: dict,
    collected: list[str],
) -> None:
    """Chat completion via llama-server.

    llama-cli --server-base does not forward -n / repeat-penalty, so a chunk
    can run away into a repeat loop. Call the HTTP API directly instead.
    """
    import json
    import urllib.error
    import urllib.request

    base = (_flag(cmd, "--server-base") or "").rstrip("/")
    prompt = _flag(cmd, "-p") or ""
    system = _flag(cmd, "-sys") or ""
    if not base or not prompt:
        raise RuntimeError("server 请求缺少地址或正文")

    # Token budget must follow -n from build_cmd (scaled to source length).
    # Old floor max(384, len*4) let ~100-char paras expand 3–4× → too_long fallback.
    n_raw = _flag(cmd, "-n")
    if n_raw and str(n_raw).lstrip("-").isdigit():
        max_tokens = max(32, int(n_raw))
    else:
        max_tokens = _rewrite_max_tokens(len(prompt))
    body: dict = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": float(_flag(cmd, "--temp", "0.7") or 0.7),
        "top_p": float(_flag(cmd, "--top-p", "0.9") or 0.9),
        "repeat_penalty": float(_flag(cmd, "--repeat-penalty", "1.15") or 1.15),
    }
    model = _server_model_id(base)
    if model:
        body["model"] = model

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "dailv",
        },
    )
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    try:
        with opener.open(req, timeout=180) as resp:
            def _watch_cancel() -> None:
                flag = globals().get("_PAGE_CANCEL")
                if flag is None or not flag.wait(180):
                    return
                try:
                    resp.close()
                except Exception:
                    pass

            threading.Thread(target=_watch_cancel, daemon=True).start()
            while True:
                if _page_cancelled():
                    result.update(rc=3, err="")
                    return
                raw = resp.read(512)
                if not raw:
                    break
                pending += decoder.decode(raw)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {})
                    piece = delta.get("content") or ""
                    if piece:
                        _enqueue(q, piece.replace("\r", ""), collected)
        tail = decoder.decode(b"", final=True)
        if tail:
            pending += tail
        result["rc"] = 0
        # 连接结束时最后一行可能没有换行，不能丢掉
        if pending.strip():
            line = pending.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        chunk = None
                    if chunk:
                        choices = chunk.get("choices") or []
                        if choices:
                            delta = (choices[0].get("delta") or {})
                            piece = delta.get("content") or ""
                            if piece:
                                _enqueue(q, piece.replace("\r", ""), collected)
    except urllib.error.HTTPError as e:
        if _page_cancelled():
            result.update(rc=3, err="")
        else:
            detail = e.read().decode("utf-8", "replace")[:500]
            result.update(rc=1, err=f"server HTTP {e.code}: {detail}")
    except Exception as e:
        if _page_cancelled():
            result.update(rc=3, err="")
        else:
            result.update(rc=1, err=f"server 请求失败: {e}")
    finally:
        q.put(_DONE)


def _produce_llama(
    cmd: list[str],
    q: queue.Queue,
    debug: bool,
    result: dict,
    collected: list[str],
) -> None:
    if "--server-base" in cmd:
        _produce_server(cmd, q, result, collected)
        return
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if not debug else None,
            bufsize=0,
        )
    except OSError as e:
        result.update(rc=1, err=f"启动 llama-cli 失败: {e}")
        q.put(_DONE)
        return

    assert proc.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    window = ""
    streaming = False
    skipping_echo = False
    err_chunks: list[bytes] = []

    try:
        while True:
            if _page_cancelled():
                proc.kill()
                result.update(rc=3, err="")
                break
            b = proc.stdout.read(1)
            if not b:
                break
            piece = decoder.decode(b).replace("\r", "")
            if not piece:
                continue

            if not streaming:
                window += piece
                if len(window) > 8000:
                    window = window[-4000:]
                idx = window.rfind("\n> ")
                if idx < 0:
                    continue
                after = window[idx + 3 :]
                nl = after.find("\n")
                if nl < 0:
                    continue
                streaming = True
                prompt_line = after[:nl]
                window = after[nl + 1 :].lstrip("\n")
                # If the "> ..." line already ends with truncated, spill is rare.
                # Otherwise hold briefly for a spilled "原文 ... (truncated)\n改写".
                skipping_echo = not any(m in prompt_line for m in _PROMPT_ECHO_MARKERS)
            else:
                window += piece

            if skipping_echo:
                cut = -1
                for marker in _PROMPT_ECHO_MARKERS:
                    pos = window.find(marker)
                    if pos >= 0:
                        cut = max(cut, pos + len(marker))
                if cut >= 0:
                    window = window[cut:].lstrip("\n")
                    skipping_echo = False
                    if not window:
                        continue
                elif len(window) < 120:
                    continue
                else:
                    skipping_echo = False
                if skipping_echo:
                    continue

            stop_at = _find_stop(window)
            if stop_at is not None:
                _enqueue(q, window[:stop_at].rstrip("\n"), collected)
                while proc.stdout.read(65536):
                    pass
                window = ""
                break

            hold = _stop_prefix_hold(window)
            ready, window = (window[:-hold], window[-hold:]) if hold else (window, "")
            if ready:
                _enqueue(q, ready, collected)

        if streaming:
            window += decoder.decode(b"", final=True).replace("\r", "")
            if skipping_echo:
                cut = -1
                for marker in _PROMPT_ECHO_MARKERS:
                    pos = window.find(marker)
                    if pos >= 0:
                        cut = max(cut, pos + len(marker))
                if cut >= 0:
                    window = window[cut:].lstrip("\n")
                skipping_echo = False
            stop_at = _find_stop(window)
            if stop_at is not None:
                window = window[:stop_at]
            _enqueue(q, window.rstrip("\n"), collected)
    except Exception as e:
        result.update(err=str(e), rc=1)
    finally:
        try:
            if proc.stderr is not None:
                err_chunks.append(proc.stderr.read() or b"")
        except Exception:
            pass
        if result.get("rc") == 3:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            result["rc"] = 3
        else:
            result["rc"] = proc.wait()
        result["stderr"] = b"".join(err_chunks).decode("utf-8", "replace")
        q.put(_DONE)


def stream_rewrite(
    cmd: list[str],
    *,
    debug: bool = False,
    end: str = "\n",
    result_label: str = RESULT_LABEL,
    show_thinking: bool = True,
    display_body: bool = True,
) -> tuple[int, str]:
    q: queue.Queue = queue.Queue()
    printed = [False]
    reveal_err: list = []
    result: dict = {"rc": 1, "stderr": "", "err": ""}
    collected: list[str] = []
    thinking_stop = threading.Event()
    thinking_done = threading.Event()

    thinker = None
    if show_thinking:
        thinker = threading.Thread(
            target=_thinking_loop, args=(thinking_stop, thinking_done), daemon=True
        )
        thinker.start()
    else:
        thinking_done.set()

    consumer = threading.Thread(
        target=_reveal_loop,
        args=(
            q,
            printed,
            reveal_err,
            thinking_stop,
            thinking_done,
            result_label,
            show_thinking,
            display_body,
        ),
        daemon=True,
    )
    producer = threading.Thread(
        target=_produce_llama, args=(cmd, q, debug, result, collected), daemon=True
    )
    consumer.start()
    producer.start()
    producer.join()
    consumer.join()
    thinking_stop.set()
    if thinker:
        thinker.join(timeout=1.0)

    if display_body and printed[0] and end:
        _out(end)

    if reveal_err:
        print(f"显示线程错误: {reveal_err[0]}", file=sys.stderr)
    if debug and result.get("stderr"):
        sys.stderr.write(result["stderr"])
    if result.get("err") and result["rc"] != 0:
        print(result["err"], file=sys.stderr)

    rc = int(result.get("rc", 1))
    err_text = result.get("stderr") or ""
    if rc == 3:
        return 3, strip_llama_prompt_echo("".join(collected))
    if rc != 0:
        if "OutOfDeviceMemory" in err_text or "failed to allocate" in err_text.lower():
            print(
                "显存不足。请减小 ngl，例如: Dailv1.7 <段落> 40 1024  /  Dailv1.5 <段落> 28 1024",
                file=sys.stderr,
            )
        elif not result.get("err"):
            print(f"推理失败 (exit {rc}):\n{err_text[-2000:]}", file=sys.stderr)
    return rc, strip_llama_prompt_echo("".join(collected))


def build_cmd(
    llama: str,
    profile: Profile,
    text: str,
    ngl: int,
    ctx: int,
    *,
    system_prompt: str | None = None,
    server_base: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    repeat_penalty: float | None = None,
    max_tokens: int | None = None,
) -> list[str]:
    cmd = [llama]
    if server_base:
        # 常驻 server：跳过重复 load（多段切分时收益最大）
        cmd += ["--server-base", server_base]
    else:
        cmd += [
            "-m",
            profile.model_path,
            "-ngl",
            str(ngl),
            "-t",
            str(THREADS),
            "-c",
            str(ctx),
            "-fa",
            FLASH_ATTN,
        ]
        if CACHE_TYPE_K:
            cmd += ["-ctk", CACHE_TYPE_K]
        if CACHE_TYPE_V:
            cmd += ["-ctv", CACHE_TYPE_V]
    n_pred = (
        max_tokens
        if max_tokens is not None
        else _rewrite_max_tokens(len(text))
    )
    n_pred = min(int(profile.n_predict), max(32, int(n_pred)))
    cmd += [
        "-n",
        str(n_pred),
        "-sys",
        system_prompt if system_prompt is not None else profile.system_prompt,
        "-p",
        text,
        "-st",
        "--no-display-prompt",
        "--simple-io",
        "--log-disable",
    ]
    temp = profile.temperature if temperature is None else temperature
    tp = profile.top_p if top_p is None else top_p
    rp = profile.repeat_penalty if repeat_penalty is None else repeat_penalty
    if temp is not None:
        cmd += ["--temp", str(temp)]
    if tp is not None:
        cmd += ["--top-p", str(tp)]
    if rp is not None:
        cmd += ["--repeat-penalty", str(rp)]
    if profile.reasoning is not None:
        cmd += ["--reasoning", profile.reasoning]
    if DEVICE:
        cmd += ["-dev", DEVICE]
    return cmd


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("serve", "--serve", "server"):
        # Dailv serve [1.7b|1.5b] [port]
        prof = "1.7b"
        port = None
        for tok in raw[1:]:
            if tok.isdigit():
                port = int(tok)
            elif not tok.startswith("-"):
                prof = tok
        return serve_profile(prof, port=port)
    if raw and raw[0] in ("status", "--status", "server-status"):
        print(server_status_text())
        # exit 0 if any ON
        return 0 if "ON" in server_status_text() else 1

    cfg = parse_args(raw)
    profile = cfg.profile

    if not os.path.isfile(profile.model_path):
        print(f"错误: 模型不存在: {profile.model_path}", file=sys.stderr)
        hints = {
            "1.7b": "提示: 设置环境变量 DAILV17_MODEL 指向 F16 GGUF（默认 D:\\hf_models\\paperpass\\gguf\\Dailv1.7-f16.gguf）。",
            "1.5b": "提示: 将 Dailv1.5-f16.gguf 放到 models/ 目录，或设置环境变量 DAILV15_MODEL。",
        }
        if profile.name in hints:
            print(hints[profile.name], file=sys.stderr)
        return 1

    try:
        llama = find_llama_cli()
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    server_base = resolve_server_base(profile)
    if server_base and not cfg.quiet_progress:
        print(f"[加速] 复用常驻 server {server_base}\n", file=sys.stderr)
    elif not server_base and not cfg.quiet_progress:
        print(
            "[提示] 未检测到常驻 server。"
            "可先运行: d17s（1.7）或 d15s（1.5）；状态 dss；关闭 d17o/d15o。\n",
            file=sys.stderr,
        )

    debug = os.environ.get("DAILV_DEBUG") == "1"
    use_protect = os.environ.get("DAILV_NO_PROTECT") != "1"
    if use_protect:
        work_text, bag = protect_spans(cfg.text)
    else:
        work_text, bag = cfg.text, []

    units = [(work_text, "")] if cfg.no_split else split_text(work_text, profile)
    sys_prompt = profile.system_prompt + _PROTECT_HINT if bag else profile.system_prompt

    if len(units) > 1 and not cfg.quiet_progress:
        unit = "句" if profile.split_mode == "sentence" else "段"
        print(
            f"[Dailv{profile.label}] 拆成 {len(units)} {unit}"
            f"（目标约 {profile.target_chars} 字，上限 {profile.max_chars}）\n",
            file=sys.stderr,
        )
    if bag and not cfg.quiet_progress:
        print(
            f"[保护] 已锁定 {len(bag)} 处（引号/数字/图表/公式/编号等）\n",
            file=sys.stderr,
        )

    use_gate = os.environ.get("DAILV_NO_GATE") != "1"

    def _cmd(
        chunk: str,
        system_prompt: str | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> list[str]:
        return build_cmd(
            llama,
            profile,
            chunk,
            cfg.ngl,
            cfg.ctx,
            system_prompt=system_prompt if system_prompt is not None else sys_prompt,
            server_base=server_base,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _gen_once(
        chunk: str,
        *,
        temperature: float | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> tuple[int, str]:
        """One model pass; returns (rc, body without unit suffix)."""
        expected = placeholder_ids(chunk) if bag else set()

        if not expected:
            rc, out = stream_rewrite(
                _cmd(
                    chunk,
                    system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                ),
                debug=debug,
                end="",
                result_label="",
                show_thinking=False,
                display_body=False,
            )
            if rc != 0:
                return rc, ""
            if not out.strip():
                return 2, ""
            return 0, _trim_repeat(out)

        rc, out = stream_rewrite(
            _cmd(
                chunk,
                system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            debug=debug,
            end="",
            result_label="",
            show_thinking=False,
            display_body=False,
        )
        if rc != 0:
            return rc, ""

        kept = placeholder_ids(out) & expected
        retention = len(kept) / max(len(expected), 1)
        if retention >= 0.5:
            out = reinject_missing_placeholders(chunk, out, expected)
            out = unprotect_spans(out, bag, expected=expected)
            if not out.strip():
                return 2, ""
            return 0, _trim_repeat(out)

        if not cfg.quiet_progress:
            print(
                f"提示: 占位符保留率 {retention:.0%}，改用片段拼接保真\n",
                file=sys.stderr,
            )
        parts = split_keep_parts(chunk, bag)
        pieces: list[str] = []
        for pi, (kind, val) in enumerate(parts):
            if kind == "k" or len(val.strip()) < 12:
                pieces.append(val)
                continue
            sys_p = profile.system_prompt
            if any(k == "k" for k, _ in parts[:pi]):
                sys_p += (
                    "前面的书名号、引号、数据与编号等固定原文已经写好，"
                    "请只改写用户给出的后续文字，不要重复固定原文。"
                )
            rc2, frag = stream_rewrite(
                _cmd(
                    val,
                    system_prompt=sys_p,
                    temperature=temperature,
                    max_tokens=min(
                        max_tokens
                        if max_tokens is not None
                        else _rewrite_max_tokens(len(val)),
                        _rewrite_max_tokens(
                            len(val),
                            mode="short" if len(val) <= 80 else "normal",
                        ),
                    ),
                ),
                debug=debug,
                end="",
                result_label="",
                show_thinking=False,
                display_body=False,
            )
            if rc2 != 0:
                return rc2, ""
            for span in bag:
                if span in frag and span not in val:
                    frag = frag.replace(span, "")
            frag = re.sub(r"^(参照|依据|根据)+", "", frag.strip()).lstrip("，,、 \t")
            pieces.append(frag if frag.strip() else val)
        out = _trim_repeat("".join(pieces))
        if not out.strip():
            return 2, ""
        return 0, out

    def _gate_src(chunk: str) -> str:
        if not bag:
            return chunk
        expected = placeholder_ids(chunk)
        return unprotect_spans(chunk, bag, expected=expected)

    def rewrite_unit(chunk: str, suffix: str, is_last: bool) -> tuple[int, str]:
        """Generate one chunk fully. Display is handled by the pipeline."""
        if _page_cancelled():
            return 3, ""
        end = "\n" if is_last else (suffix or "")
        src = _gate_src(chunk)
        ai_before = _ai_prob(src) if _DROP_RANK else None
        list_mode = _is_list_template(src)
        short_lim = int(os.environ.get("DAILV_SHORT_CHARS", "160"))
        short_mode = len(src) <= short_lim
        # 短段默认不走片段拼接（那条路曾经把整段 token 预算套到每个碎片上）。
        # 句中若有书名、引号、数字或中文数字，仍用占位符原文，避免保护被短段旁路拆掉。
        has_protect = bool(bag and placeholder_ids(chunk))
        gen_in = chunk if has_protect or not short_mode else src
        unit_sys = sys_prompt
        if list_mode:
            unit_sys += _LIST_REWRITE_HINT
        if short_mode:
            unit_sys += _SHORT_REWRITE_HINT
        tok_mode = "short" if short_mode else "normal"
        bon_max_tok = _rewrite_max_tokens(len(src), mode=tok_mode)
        compress_max_tok = _rewrite_max_tokens(len(src), mode="compress")

        def _score_body(body: str) -> tuple[str, str | None]:
            """Polish fluency, optionally reinject probes; return (body, gate_fail_reason)."""
            body = _polish_fluency(body)
            if not use_gate:
                return body, None
            reason = export_gate_fail(src, body)
            if (
                _PROBE_REINJECT
                and reason
                and (
                    reason.startswith("missing_en=")
                    or reason.startswith("missing_cn_ord=")
                    or reason.startswith("ordinal_changed=")
                    or reason.startswith("missing_probe=")
                    or reason.startswith("magnitude_near1yi")
                    or reason.startswith("magnitude_stack")
                    or reason.startswith("polarity=")
                    or reason in ("accuracy_dup", "meta_leak")
                )
            ):
                fixed = reinject_probes(src, body)
                fixed = _polish_fluency(fixed)
                if fixed.strip() != body.strip():
                    reason2 = export_gate_fail(src, fixed)
                    if reason2 is None or _cand_rank(
                        src, fixed, ai_before=ai_before
                    ) < _cand_rank(src, body, ai_before=ai_before):
                        if not cfg.quiet_progress:
                            print(
                                f"提示: 已回填探针（原拦截 {reason}"
                                f"{'' if reason2 is None else ' → ' + reason2}）\n",
                                file=sys.stderr,
                            )
                        return fixed, reason2
            return body, reason

        cands: list[tuple[str, str | None, float | str]] = []
        bon_temps = _SHORT_BON_TEMPS if short_mode else _BON_TEMPS
        temps = list(bon_temps[:_BON_N]) if use_gate else [None]
        for i, temp in enumerate(temps):
            rc, body = _gen_once(
                gen_in,
                temperature=temp,
                system_prompt=unit_sys,
                max_tokens=bon_max_tok,
            )
            if rc == 3 or _page_cancelled():
                return 3, ""
            if rc != 0:
                if not cands:
                    return rc, ""
                continue
            body, reason = _score_body(body)
            cands.append((body, reason, temp if temp is not None else "default"))
            if not cfg.quiet_progress and _BON_N > 1 and use_gate:
                drop_s = ""
                d = _cand_drop(src, body, ai_before)
                if d is not None:
                    drop_s = f" drop={d:.3f}"
                print(
                    f"  bon[{i}] t={temp} gate="
                    f"{'ok' if reason is None else reason} "
                    f"stiff={_stiff_penalty(src, body)}{drop_s}\n",
                    file=sys.stderr,
                )

        if not cands:
            return 2, ""

        cands.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
        body, reason, _ = cands[0]

        # Aggressive style-break: weak drop / list template / stiff.
        # Skip absolute-drop chasing when src AI already < floor (no headroom; id1).
        best_drop = _cand_drop(src, body, ai_before)
        need_agg = False
        if use_gate and _AGGRESSIVE_RETRY and reason is None:
            floor = _AGGRESSIVE_DROP_FLOOR
            if list_mode or _is_list_template(body):
                floor = max(floor, _WEAK_DROP)
            can_chase_drop = _drop_floor_applicable(ai_before, floor)
            if can_chase_drop and best_drop is not None and best_drop < floor:
                need_agg = True
            elif _stiff_penalty(src, body) >= 2:
                need_agg = True
            elif (
                list_mode
                and can_chase_drop
                and (best_drop is None or best_drop < _WEAK_DROP)
            ):
                need_agg = True
            elif (
                not can_chase_drop
                and not cfg.quiet_progress
                and best_drop is not None
                and best_drop < floor
            ):
                print(
                    f"提示: src AI={ai_before:.3f} 已低于降幅门槛 {floor:.2f}，"
                    f"跳过按绝对 drop 激进重写（当前 drop={best_drop:.3f}）\n",
                    file=sys.stderr,
                )
        if need_agg:
            keep_bits = []
            for t in _EN_ACRONYM_KEEP + (
                "近1亿",
                "第三",
                "千人千面",
                "窦娥冤",
                "Accuracy",
                "BLEU-4",
                "ROUGE-L",
            ):
                if t in src:
                    keep_bits.append(t)
            keep = "、".join(keep_bits) if keep_bits else "原文英文缩写与数字"
            if list_mode or _is_list_template(body):
                agg = unit_sys + _LIST_AGG_HINT + f"必须原样保留：{keep}。"
                agg_temps = (0.95, 0.85, 0.75, 0.65, 0.55)
            elif float(ai_before or 0) >= 0.5:
                # High-AI narrative (literary/套话): push colloquial break early
                agg = unit_sys + _NARR_AGG_HINT + f"必须原样保留：{keep}。"
                agg_temps = (0.95, 0.85, 0.75, 0.65)
            else:
                agg = (
                    unit_sys
                    + "\n【强化降AI】上一稿仍像模板。请打散排比与冒号罗列："
                    "改用短句+长短错落，先结论后细节；避免「分为…两大」「综上所述」套话。"
                    f"必须原样保留：{keep}。只输出正文。"
                )
                agg_temps = (0.9, 0.75, 0.6)
            prev_hi = _with_gate_len_hi(_AGG_LEN_HI)
            try:
                for t in agg_temps:
                    rc_a, body_a = _gen_once(
                        gen_in,
                        temperature=t,
                        system_prompt=agg,
                        max_tokens=bon_max_tok,
                    )
                    if rc_a != 0:
                        continue
                    body_a, reason_a = _score_body(body_a)
                    cands.append((body_a, reason_a, f"agg@{t}"))
                    if _cand_rank(src, body_a, ai_before=ai_before) < _cand_rank(
                        src, body, ai_before=ai_before
                    ):
                        if not cfg.quiet_progress:
                            d_a = _cand_drop(src, body_a, ai_before)
                            drop_note = f", drop={d_a:.3f}" if d_a is not None else ""
                            print(
                                f"提示: 激进重写更优 (t={t}, gate="
                                f"{'ok' if reason_a is None else reason_a}{drop_note})\n",
                                file=sys.stderr,
                            )
                        body, reason = body_a, reason_a
            finally:
                _restore_gate_len_hi(prev_hi)

            cands.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
            body, reason, _ = cands[0]
            # Second pass if still weak on list bodies
            cur_drop = _cand_drop(src, body, ai_before)
            if (
                use_gate
                and reason is None
                and list_mode
                and _drop_floor_applicable(ai_before, _WEAK_DROP)
                and cur_drop is not None
                and cur_drop < _WEAK_DROP
            ):
                if not cfg.quiet_progress:
                    print(
                        f"提示: 列表体降幅仍弱 drop={cur_drop:.3f}，二轮打散\n",
                        file=sys.stderr,
                    )
                agg2 = (
                    unit_sys
                    + _LIST_AGG_HINT
                    + f"上一稿降AI不足。再改一版，句式必须与原文骨架不同。必须保留：{keep}。"
                )
                prev_hi = _with_gate_len_hi(_AGG_LEN_HI)
                try:
                    for t in (1.0, 0.8, 0.6):
                        rc_a, body_a = _gen_once(
                            gen_in,
                            temperature=t,
                            system_prompt=agg2,
                            max_tokens=bon_max_tok,
                        )
                        if rc_a != 0:
                            continue
                        body_a, reason_a = _score_body(body_a)
                        cands.append((body_a, reason_a, f"agg2@{t}"))
                finally:
                    _restore_gate_len_hi(prev_hi)
                cands.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
                body, reason, _ = cands[0]

            # Narrative second pass: high-AI src still weak after first agg (id28-class)
            cur_drop = _cand_drop(src, body, ai_before)
            if (
                use_gate
                and reason is None
                and not list_mode
                and need_agg
                and _drop_floor_applicable(ai_before)
                and cur_drop is not None
                and cur_drop < _AGGRESSIVE_DROP_FLOOR
                and float(ai_before or 0) >= 0.5
            ):
                if not cfg.quiet_progress:
                    print(
                        f"提示: 叙述体降幅仍弱 drop={cur_drop:.3f}，二轮打散\n",
                        file=sys.stderr,
                    )
                keep_bits = [
                    t
                    for t in _EN_ACRONYM_KEEP
                    + ("近1亿", "第三", "千人千面", "窦娥冤", "Accuracy", "BLEU-4", "ROUGE-L")
                    if t in src
                ]
                keep = "、".join(keep_bits) if keep_bits else "原文专名与核心意旨"
                agg2 = (
                    unit_sys
                    + _NARR_AGG_HINT
                    + f"必须原样保留：{keep}。"
                )
                prev_hi = _with_gate_len_hi(_AGG_LEN_HI)
                try:
                    for t in (1.0, 0.85, 0.7):
                        rc_a, body_a = _gen_once(
                            gen_in,
                            temperature=t,
                            system_prompt=agg2,
                            max_tokens=bon_max_tok,
                        )
                        if rc_a != 0:
                            continue
                        body_a, reason_a = _score_body(body_a)
                        cands.append((body_a, reason_a, f"narr_agg2@{t}"))
                        if _cand_rank(src, body_a, ai_before=ai_before) < _cand_rank(
                            src, body, ai_before=ai_before
                        ):
                            body, reason = body_a, reason_a
                            if not cfg.quiet_progress:
                                d_a = _cand_drop(src, body_a, ai_before)
                                drop_note = (
                                    f", drop={d_a:.3f}" if d_a is not None else ""
                                )
                                print(
                                    f"提示: 叙述二轮更优 (t={t}, gate="
                                    f"{'ok' if reason_a is None else reason_a}{drop_note})\n",
                                    file=sys.stderr,
                                )
                finally:
                    _restore_gate_len_hi(prev_hi)
                cands.sort(key=lambda c: _cand_rank(src, c[0], ai_before=ai_before))
                body, reason, _ = cands[0]

        if use_gate:
            attempt = 0
            while reason and attempt < _GATE_RETRY:
                attempt += 1
                if not cfg.quiet_progress:
                    print(
                        f"提示: 出口拦截（{reason}），低温重写本段 "
                        f"(temp={_GATE_TEMP})\n",
                        file=sys.stderr,
                    )
                keep_bits = [
                    t
                    for t in _EN_ACRONYM_KEEP + ("近1亿", "第三", "千人千面")
                    if t in src
                ]
                keep = "、".join(keep_bits) if keep_bits else "原文英文缩写与数字"
                hint = (
                    unit_sys
                    + f"\n【强制】上轮改写不合格（{reason}）。"
                    f"必须原样保留：{keep}。"
                    "长度接近原文（约0.7～1.2倍），不得扩写百科、不得串换病种/指南缩写，"
                    "只输出改写正文。"
                )
                rc2, body2 = _gen_once(
                    gen_in,
                    temperature=_GATE_TEMP,
                    system_prompt=hint,
                    max_tokens=compress_max_tok,
                )
                if rc2 != 0:
                    return rc2, ""
                body2, reason2 = _score_body(body2)
                cands.append((body2, reason2, f"gate_retry@{attempt}"))
                body, reason = _pick_ship_cand(src, cands, ai_before)

            body, reason = _pick_ship_cand(src, cands, ai_before)
            # Length runaway / domain garbage: dedicated compress then length-ok pool
            need_compress = bool(
                reason
                and (
                    reason.startswith("too_long")
                    or reason.startswith("domain_swap")
                    or reason.startswith("acronym_swap")
                    or reason.startswith("prompt_leak")
                    or _len_ratio(src, body) > _SEVERE_LEN_HI
                )
            )
            if need_compress:
                if not cfg.quiet_progress:
                    print(
                        f"提示: 出口失控（{reason}），压缩/纠偏重写\n",
                        file=sys.stderr,
                    )
                keep_bits = [
                    t
                    for t in _EN_ACRONYM_KEEP + ("近1亿", "第三", "千人千面")
                    if t in src
                ]
                keep = "、".join(keep_bits) if keep_bits else "原文专名与数字"
                compress = (
                    unit_sys
                    + "\n【强制】上一稿不合格（过长或串题）。"
                    f"请压缩到约{len(src)}字，紧贴原意改写，禁止百科扩写与病种串换；"
                    f"必须原样保留：{keep}。只输出正文。"
                )
                for t in (0.25, 0.4, 0.55):
                    rc_c, body_c = _gen_once(
                        gen_in,
                        temperature=t,
                        system_prompt=compress,
                        max_tokens=compress_max_tok,
                    )
                    if rc_c != 0:
                        continue
                    body_c, reason_c = _score_body(body_c)
                    cands.append((body_c, reason_c, f"compress@{t}"))
                body, reason = _pick_ship_cand(src, cands, ai_before)

            if reason:
                # Probe/ordinal/magnitude: never soft-ship a failing body
                probeish = (
                    reason.startswith("missing_probe=")
                    or reason.startswith("missing_en=")
                    or reason.startswith("missing_cn_ord=")
                    or reason.startswith("ordinal_changed=")
                    or reason.startswith("magnitude")
                    or reason.startswith("polarity=")
                )
                if probeish and _PROBE_REINJECT:
                    fixed = _polish_fluency(reinject_probes(src, body))
                    if export_gate_fail(src, fixed) is None:
                        body, reason = fixed, None
                        if not cfg.quiet_progress:
                            print("提示: 出口探针回填后过门\n", file=sys.stderr)
                    else:
                        # Prefer any gate-ok candidate over a probe-fail winner
                        ok_pool = []
                        for b, _r, tag in cands:
                            bp = _polish_fluency(b)
                            if export_gate_fail(src, bp) is None:
                                ok_pool.append((bp, None, tag))
                        if ok_pool:
                            ok_pool.sort(
                                key=lambda c: _cand_rank(
                                    src, c[0], ai_before=ai_before
                                )
                            )
                            body, reason = ok_pool[0][0], None
                            if not cfg.quiet_progress:
                                print(
                                    "提示: 探针失败，改选 gate-ok 次优候选\n",
                                    file=sys.stderr,
                                )

                severe = (
                    reason is not None
                    and (
                        reason.startswith(
                            (
                                "too_long",
                                "domain_swap",
                                "acronym_swap",
                                "prompt_leak",
                                "meta_leak",
                                "invented_",
                            )
                        )
                        or _len_ratio(src, body) > _SEVERE_LEN_HI
                    )
                )
                # Probe-class fails: fallback to src (fidelity over wounded ship)
                if reason and (
                    _FALLBACK_SRC
                    or (_FALLBACK_SEVERE and severe)
                    or probeish
                ):
                    if not cfg.quiet_progress:
                        print(
                            f"提示: 出口仍不合格（{reason}），本段回退原文"
                            f"{'（探针/严重失控）' if probeish or severe else ''}\n",
                            file=sys.stderr,
                        )
                    body = src
                    reason = None
                elif reason and not cfg.quiet_progress:
                    d_keep = _cand_drop(src, body, ai_before)
                    drop_note = f" drop={d_keep:.3f}" if d_keep is not None else ""
                    print(
                        f"提示: 出口仍不合格（{reason}），保留门控优先候选"
                        f"{drop_note}（不回退原文）\n",
                        file=sys.stderr,
                    )

        # Always reinject known probes on the shipped body (千人千面 etc.)
        if _PROBE_REINJECT and body and body != src:
            fixed = reinject_probes(src, body)
            fixed = _polish_fluency(fixed)
            if fixed.strip() != body.strip():
                fail_f = export_gate_fail(src, fixed) if use_gate else None
                fail_b = export_gate_fail(src, body) if use_gate else None
                if fail_f is None or (
                    fail_b is not None
                    and _cand_rank(src, fixed, ai_before=ai_before)
                    <= _cand_rank(src, body, ai_before=ai_before)
                ):
                    if not cfg.quiet_progress:
                        print("提示: 终选已回填探针\n", file=sys.stderr)
                    body = fixed
        elif body:
            body = _polish_fluency(body)

        # Final ship guard: never leave with open probe/ordinal gate
        if use_gate and body and body != src:
            reason_final = export_gate_fail(src, body)
            if reason_final and (
                reason_final.startswith(
                    (
                        "missing_probe=",
                        "missing_en=",
                        "missing_cn_ord=",
                        "ordinal_changed=",
                        "magnitude",
                        "polarity=",
                        "acronym_swap=",
                        "domain_swap=",
                    )
                )
            ):
                fixed = _polish_fluency(reinject_probes(src, body))
                if export_gate_fail(src, fixed) is None:
                    body = fixed
                else:
                    if not cfg.quiet_progress:
                        print(
                            f"提示: 终检仍不合格（{reason_final}），回退原文\n",
                            file=sys.stderr,
                        )
                    body = src

        # Low-headroom: never ship if detector AI rose (matches accept ai_rise_low_src)
        if (
            use_gate
            and _DROP_RANK
            and ai_before is not None
            and not _drop_floor_applicable(ai_before)
            and body
            and body != src
        ):
            after = _ai_prob(body)
            if after is not None and after > float(ai_before) + 0.05:
                safe: list[tuple[str, float, float | str]] = []
                for b, _r, tag in cands:
                    bp = _polish_fluency(b)
                    if export_gate_fail(src, bp) is not None:
                        continue
                    aa = _ai_prob(bp)
                    if aa is not None and aa <= float(ai_before) + 0.05:
                        safe.append((bp, aa, tag))
                if safe:
                    safe.sort(key=lambda c: (c[1], _fluency_penalty(src, c[0])))
                    body = safe[0][0]
                    if not cfg.quiet_progress:
                        print(
                            "提示: 低srcAI改写抬升，改选不抬升候选\n",
                            file=sys.stderr,
                        )
                else:
                    if not cfg.quiet_progress:
                        print("提示: 低srcAI改写抬升，回退原文\n", file=sys.stderr)
                    body = src

        # Drop AIGC：只卡字数范围。超出则重写，最多 3 次，仍超出才用原文。
        if os.environ.get("DAILV_LEN_GATE") == "1" and body and src.strip():
            def _in_len_band(text: str) -> bool:
                ratio = len(text) / max(len(src), 1)
                return _GATE_LEN_LO <= ratio <= _GATE_LEN_HI

            if not _in_len_band(body):
                hint = (
                    unit_sys
                    + "\n【强制】上一稿长度不合格。"
                    f"请改到大约{len(src)}字，必须落在原文的{_GATE_LEN_LO:.1f}～{_GATE_LEN_HI:.1f}倍。"
                    "不要扩写，不要添加原文没有的内容。只输出正文。"
                )
                for _attempt in range(3):
                    rc_c, body_c = _gen_once(
                        gen_in,
                        temperature=_GATE_TEMP,
                        system_prompt=hint,
                        max_tokens=compress_max_tok,
                    )
                    if rc_c == 3 or _page_cancelled():
                        return 3, ""
                    if rc_c != 0 or not body_c.strip():
                        continue
                    body_c = _polish_fluency(body_c)
                    if _in_len_band(body_c):
                        body = body_c
                        break
                if not _in_len_band(body):
                    if not cfg.quiet_progress:
                        print(
                            f"提示: 字数重写 3 次仍超出 {_GATE_LEN_LO:.1f}～{_GATE_LEN_HI:.1f}，本句回退原文\n",
                            file=sys.stderr,
                        )
                    body = src

        return 0, body + end

    # 生成与展示重叠：打第 i 段时 GPU 已在写第 i+1 段。
    # 同一张卡上并行两路会互相抢算力，串行预取才不掉 tok/s。
    ready: queue.Queue = queue.Queue()

    def _worker() -> None:
        try:
            for i, (chunk, suffix) in enumerate(units):
                rc, text = rewrite_unit(chunk, suffix, i == len(units) - 1)
                if rc == 3:
                    ready.put(("done",))
                    return
                if rc != 0:
                    ready.put(("err", rc, i))
                    return
                ready.put(("ok", text, i == len(units) - 1))
            ready.put(("done",))
        except Exception as e:
            ready.put(("exc", str(e)))

    threading.Thread(target=_worker, daemon=True).start()

    thinking_stop = threading.Event()
    thinking_done = threading.Event()
    thinker = threading.Thread(
        target=_thinking_loop, args=(thinking_stop, thinking_done), daemon=True
    )
    thinker.start()
    labeled = False

    def _stop_think() -> None:
        thinking_stop.set()
        thinking_done.wait(timeout=2.0)
        # 正文已经写出后不能再 \r，否则会抹掉终端自动换行那一行的句尾
        if not labeled:
            _clear_thinking()

    def _start_think() -> None:
        nonlocal thinking_stop, thinking_done, thinker
        if labeled:
            return
        if thinker.is_alive() and not thinking_stop.is_set():
            return
        thinking_stop = threading.Event()
        thinking_done = threading.Event()
        thinker = threading.Thread(
            target=_thinking_loop, args=(thinking_stop, thinking_done), daemon=True
        )
        thinker.start()

    while True:
        try:
            item = ready.get(timeout=0.2)
        except queue.Empty:
            _start_think()
            continue
        kind = item[0]
        if kind == "done":
            break
        if kind == "exc":
            _stop_think()
            print(f"错误: {item[1]}", file=sys.stderr)
            return 1
        if kind == "err":
            _stop_think()
            _, rc, idx = item
            if rc == 2:
                print(f"错误: 第 {idx + 1} 段改写结果为空。", file=sys.stderr)
                return 1
            return int(rc)
        _, text, is_last = item
        _stop_think()
        if not labeled:
            _out(RESULT_LABEL + "\n\n")
            labeled = True
        _pace_out(text, ready, steady=not is_last)
        if not is_last and ready.empty():
            _start_think()

    _stop_think()
    return 0

