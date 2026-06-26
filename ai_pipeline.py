"""
ai_pipeline.py — 5-stage AI Pipeline for product copy generation

Stages:
  1. Product Analyzer  — 分析商品特性，輸出 JSON
  2. Brand Profile     — 載入品牌設定（無 API call）
  3. Search Intent     — 分析搜尋意圖（與 4 平行執行）
  4. Competitor        — 競品分析（與 3 平行執行）
  5. Copy Generator    — 依據前四階段生成文案

控制開關：
  環境變數 USE_AI_PIPELINE=true  → 全域預設啟用
  run_pipeline(use_pipeline=True) → 每次呼叫可覆蓋

Fallback：
  任一階段失敗時，run_pipeline 回傳 error，
  由呼叫端（_run_ai_rewrite_for_job）自動 fallback 到舊 _ai_rewrite。
"""

import os, json, time, threading, sys, urllib.request, re

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_AI_PIPELINE   = os.getenv("USE_AI_PIPELINE", "false").lower() == "true"
_MODEL            = "claude-haiku-4-5-20251001"


# ── Claude API helper ─────────────────────────────────────────────
def _claude_call(prompt, max_tokens=2048):
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 未設定")
    req_data = json.dumps({
        "model": _MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=req_data, method="POST",
        headers={
            "x-api-key":           ANTHROPIC_API_KEY,
            "anthropic-version":   "2023-06-01",
            "content-type":        "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read().decode())
    return resp["content"][0]["text"].strip()


def _parse_json(text):
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        return json.loads(m.group())
    raise ValueError(f"無法解析 JSON 回應：{text[:300]}")


# ── Stage 1: Product Analyzer ─────────────────────────────────────
def agent_product_analyzer(raw_title, raw_desc, raw_price=""):
    from prompts import product_analyzer
    prompt = product_analyzer.build_prompt(raw_title, raw_desc, raw_price)
    text = _claude_call(prompt, max_tokens=1024)
    return _parse_json(text)


# ── Stage 2: Brand Profile（無 API call）─────────────────────────
def agent_brand_profile(brand_key):
    from prompts.brands import get_brand_profile
    profile = get_brand_profile(brand_key)
    if profile:
        return profile
    # generic fallback
    return {
        "name":           "台灣電商品牌",
        "category":       "商品",
        "positioning":    "台灣官網品質，直接說明功能。",
        "style":          "簡潔、專業。",
        "tone":           "直接說明功能，不像業務推銷。",
        "target_audience":"台灣消費者",
        "forbidden_words":"喔、恩、那個、就是說、其實、基本上、保證、一定有效、絕對",
        "faq_strategy":   "購買前常見疑問",
        "seo_strategy":   "Google 搜尋導向",
        "copy_length":    "desc 200-400字",
        "aeo_rules":      "FAQ 問句符合 Google AEO 格式",
    }


# ── Stage 3: Search Intent ────────────────────────────────────────
def agent_search_intent(product_analysis):
    from prompts import search_intent
    prompt = search_intent.build_prompt(product_analysis)
    text = _claude_call(prompt, max_tokens=1024)
    return _parse_json(text)


# ── Stage 4: Competitor Analyzer ─────────────────────────────────
def agent_competitor_analyzer(product_analysis):
    from prompts import competitor_analyzer
    prompt = competitor_analyzer.build_prompt(product_analysis)
    text = _claude_call(prompt, max_tokens=1024)
    return _parse_json(text)


# ── Stage 5: Copy Generator ───────────────────────────────────────
def agent_copy_generator(brand_profile, product_analysis, search_intent_data, competitor_data):
    from prompts import copy_generator
    prompt = copy_generator.build_prompt(
        brand_profile, product_analysis, search_intent_data, competitor_data
    )
    text   = _claude_call(prompt, max_tokens=2048)
    result = _parse_json(text)
    return {
        "name":         result.get("name", ""),
        "desc":         result.get("desc", ""),
        "keywords":     result.get("keywords", ""),
        "shopee_title": result.get("shopee_title", ""),
        "website_name": result.get("website_name", ""),
        "features":     result.get("features", ""),
        "price_min":    str(result.get("price_min", "") or ""),
        "price_max":    str(result.get("price_max", "") or ""),
        "seo_desc":     result.get("seo_desc", ""),
        "faq":          result.get("faq", []) if isinstance(result.get("faq"), list) else [],
    }


# ── Main Pipeline ─────────────────────────────────────────────────
def run_pipeline(raw_title, raw_desc, raw_price="", brand_key=""):
    """
    執行 5 階段 pipeline。

    回傳 dict：
    {
        "copy":               {...},   # 同 _ai_rewrite 輸出格式
        "analysis_json":      {...},   # Stage 1
        "search_intent_json": {...},   # Stage 3
        "competitor_json":    {...},   # Stage 4
        "pipeline_log":       [...],   # 每步驟 log
        "error":              None or "錯誤訊息"
    }
    失敗時 error 非 None，由呼叫端決定是否 fallback。
    """
    log = []

    def _log(stage, msg):
        entry = f"[{stage}] {msg}"
        log.append(entry)
        print(entry, file=sys.stderr)

    result = {
        "copy":               None,
        "analysis_json":      None,
        "search_intent_json": None,
        "competitor_json":    None,
        "pipeline_log":       log,
        "error":              None,
    }

    # ── Stage 1 ──────────────────────────────────────────────────
    try:
        _log("1-ProductAnalyzer", "開始分析...")
        t0 = time.time()
        analysis = agent_product_analyzer(raw_title, raw_desc, raw_price)
        result["analysis_json"] = analysis
        _log("1-ProductAnalyzer", f"完成 ({time.time()-t0:.1f}s) type={analysis.get('product_type','?')}")
    except Exception as e:
        result["error"] = f"Stage1 ProductAnalyzer 失敗：{e}"
        _log("1-ProductAnalyzer", f"失敗：{e}")
        return result

    # ── Stage 2（無 API call）────────────────────────────────────
    brand_profile = agent_brand_profile(brand_key)
    _log("2-BrandProfile", f"載入品牌：{brand_profile.get('name', 'generic')}")

    # ── Stage 3 & 4 平行執行 ─────────────────────────────────────
    si_result, ca_result = [None], [None]
    si_error,  ca_error  = [None], [None]

    def _run_si():
        try:
            t = time.time()
            si_result[0] = agent_search_intent(analysis)
            _log("3-SearchIntent", f"完成 ({time.time()-t:.1f}s)")
        except Exception as e:
            si_error[0] = str(e)
            _log("3-SearchIntent", f"失敗：{e}")

    def _run_ca():
        try:
            t = time.time()
            ca_result[0] = agent_competitor_analyzer(analysis)
            _log("4-Competitor", f"完成 ({time.time()-t:.1f}s)")
        except Exception as e:
            ca_error[0] = str(e)
            _log("4-Competitor", f"失敗：{e}")

    _log("3-SearchIntent", "開始（平行）...")
    _log("4-Competitor",   "開始（平行）...")
    t34 = time.time()
    th_si = threading.Thread(target=_run_si, daemon=True)
    th_ca = threading.Thread(target=_run_ca, daemon=True)
    th_si.start(); th_ca.start()
    th_si.join(timeout=90); th_ca.join(timeout=90)
    _log("3+4-Parallel", f"平行完成 ({time.time()-t34:.1f}s)")

    if si_error[0]:
        result["error"] = f"Stage3 SearchIntent 失敗：{si_error[0]}"
        return result
    if ca_error[0]:
        result["error"] = f"Stage4 Competitor 失敗：{ca_error[0]}"
        return result

    result["search_intent_json"] = si_result[0]
    result["competitor_json"]    = ca_result[0]

    # ── Stage 5 ──────────────────────────────────────────────────
    try:
        _log("5-CopyGenerator", "開始生成文案...")
        t5 = time.time()
        copy = agent_copy_generator(brand_profile, analysis, si_result[0], ca_result[0])
        result["copy"] = copy
        _log("5-CopyGenerator", f"完成 ({time.time()-t5:.1f}s)")
    except Exception as e:
        result["error"] = f"Stage5 CopyGenerator 失敗：{e}"
        _log("5-CopyGenerator", f"失敗：{e}")
        return result

    _log("Pipeline", "全部完成")
    return result
